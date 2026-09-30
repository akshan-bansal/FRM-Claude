"""Card-signed baskets: which symbols a venue may open new positions in.

A basket is the human's answer to "which names may this venue trade at all?", made before any
individual trade is put in front of the card. It is signed by the same Ed25519 card key that signs
trade intents, over canonical bytes that bind the venue, the sorted symbol list and a hash of the
analysis the human was shown (WYSIWYS, as for trades).

The Router gate is an ADDITIONAL gate. It never loosens one that exists. It rejects entries only:
exits reduce exposure and are always allowed. A venue with no valid signed basket takes no new
entries, and a row that fails signature or canonical re-verification counts as no row, so editing
``state/baskets.jsonl`` by hand cannot approve anything.

Card firmware does not sign baskets yet: its parser reads the nine-field trade canonical and would
refuse this one (the safe failure). Today the simulated card signs them through ``scripts/basket.py``.
Firmware needs a ``BASKET|1|...`` prompt kind before a physical card can, plus a SPEC_VERSION bump.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BASKET_SPEC = "1"
DEFAULT_PATH = Path("state") / "baskets.jsonl"


def normalize(symbols: list[str] | tuple[str, ...] | frozenset[str]) -> tuple[str, ...]:
    # A leading "/" marks a future in this repo (/GC); the same contract arrives as GC from the IB
    # feed, so both spell one basket member. Kraken's inner slash (BTC/USD) is untouched.
    return tuple(sorted({s.strip().upper().lstrip("/") for s in symbols if s and s.strip()}))


def ib_refusal(symbol: str, universe_path: Path = Path("config/futures_universe.json")) -> str | None:
    """Why ``symbol`` cannot be in an IB basket: the IB book carries listed futures only.

    A symbol qualifies when its root is a contract in the futures universe, spelled with or without
    the leading slash. Equities trade on Questrade (desk venue split).
    """
    try:
        roots = {str(c["symbol"]).upper().lstrip("/")
                 for c in json.loads(universe_path.read_text(encoding="utf-8"))["contracts"]}
    except (OSError, ValueError, KeyError, TypeError):
        return f"cannot read the futures universe at {universe_path}; IB baskets need it"
    if symbol.strip().upper().lstrip("/") in roots:
        return None
    return f"{symbol} is not in the futures universe; IB carries futures only (equities trade on Questrade)"


def analysis_hash(analysis: Any) -> str:
    """Stable digest of the analysis blob the human was shown. Empty analysis hashes to ''."""
    if not analysis:
        return ""
    blob = json.dumps(analysis, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def canonical_basket(*, venue: str, symbols: tuple[str, ...], analysis_digest: str,
                     issued_at: str, nonce: str) -> bytes:
    """Bytes the card signs. Symbols are sorted and upper-cased so order can't change the bytes."""
    return "|".join(["BASKET", BASKET_SPEC, venue.lower(), ",".join(normalize(symbols)),
                     analysis_digest, issued_at, nonce]).encode("utf-8")


def fingerprint(canonical: bytes, chars: int = 8) -> str:
    d = hashlib.sha256(canonical).hexdigest().upper()
    return f"{d[: chars // 2]}...{d[-(chars // 2):]}"


PROPOSAL_MAX_AGE_S = 1800


def make_proposal(*, venue: str, symbols: list[str], analysis: Any) -> dict[str, Any]:
    """A basket waiting for the card. The nonce and time are fixed HERE, so the fingerprint the
    panel shows is the fingerprint of the exact bytes the card will be asked to sign."""
    syms = normalize(symbols)
    issued, nonce = datetime.now(UTC).isoformat(), secrets.token_hex(8)
    digest = analysis_hash(analysis)
    canon = canonical_basket(venue=venue, symbols=syms, analysis_digest=digest,
                             issued_at=issued, nonce=nonce)
    return {"id": nonce, "venue": venue.lower(), "symbols": list(syms), "analysis": analysis,
            "analysis_hash": digest, "issued_at": issued, "nonce": nonce,
            "canonical": canon.decode("utf-8"), "fingerprint": fingerprint(canon)}


def proposal_age_s(p: dict[str, Any]) -> float:
    return (datetime.now(UTC) - datetime.fromisoformat(str(p["issued_at"]))).total_seconds()


def sign_proposal(p: dict[str, Any], *, card_id: str, sign: Callable[[bytes], bytes]) -> dict[str, Any]:
    """Turn a proposal into the stored row. Refuses a proposal older than PROPOSAL_MAX_AGE_S: the
    analysis the human looked at is stale by then, and they should be shown a fresh one."""
    if proposal_age_s(p) > PROPOSAL_MAX_AGE_S:
        raise ValueError("proposal is older than 30 minutes; propose it again on fresh analysis")
    canon = p["canonical"].encode("utf-8")
    return {"kind": "basket", "venue": p["venue"], "symbols": list(p["symbols"]),
            "analysis_hash": p["analysis_hash"], "issued_at": p["issued_at"], "nonce": p["nonce"],
            "card_id": card_id, "canonical": p["canonical"],
            "signature": base64.b64encode(sign(canon)).decode()}


def new_row(*, venue: str, symbols: list[str], analysis: Any, card_id: str,
            sign: Callable[[bytes], bytes]) -> dict[str, Any]:
    """Propose and sign in one step (the CLI's direct path)."""
    return sign_proposal(make_proposal(venue=venue, symbols=symbols, analysis=analysis),
                         card_id=card_id, sign=sign)


def append_proposal(p: dict[str, Any], path: Path) -> None:
    append_row(p, path)


def pending_proposals(path: Path, book: BasketBook | None = None) -> list[dict[str, Any]]:
    """Unsigned, unexpired proposals, newest first. A proposal is signed once a verified row with
    its nonce exists in the book file."""
    if not path.exists():
        return []
    props = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    signed = set()
    bpath = path.with_name("baskets.jsonl")
    if bpath.exists():
        for ln in bpath.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError):
                signed.add(json.loads(ln).get("nonce"))
    out = [p for p in props if p.get("nonce") not in signed and proposal_age_s(p) <= PROPOSAL_MAX_AGE_S]
    return sorted(out, key=lambda p: p["issued_at"], reverse=True)


def append_row(row: dict[str, Any], path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


@dataclass(frozen=True)
class Approved:
    venue: str
    symbols: frozenset[str]
    issued_at: str
    card_id: str
    analysis_hash: str


class BasketBook:
    """Reads ``state/baskets.jsonl`` and returns, per venue, the latest row that verifies.

    ``verify(card_id, canonical, signature)`` is the card registry's. A row is valid only if the
    signature verifies AND its canonical bytes equal the ones rebuilt from its own fields, so a
    row whose displayed symbols differ from the signed ones is rejected.
    """

    def __init__(self, path: Path, verify: Callable[[str, bytes, bytes], bool]) -> None:
        self.path = Path(path)
        self._verify = verify
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._by_venue: dict[str, Approved] = {}
        self.rejected_rows = 0

    def _valid(self, r: dict[str, Any]) -> Approved | None:
        try:
            venue, syms = str(r["venue"]).lower(), normalize(r["symbols"])
            canon = canonical_basket(venue=venue, symbols=syms, analysis_digest=str(r["analysis_hash"]),
                                     issued_at=str(r["issued_at"]), nonce=str(r["nonce"]))
            if canon.decode("utf-8") != r["canonical"]:
                return None
            sig = base64.b64decode(r["signature"], validate=True)
            if not self._verify(str(r["card_id"]), canon, sig):
                return None
        except (KeyError, TypeError, ValueError, binascii.Error):
            return None
        return Approved(venue, frozenset(syms), str(r["issued_at"]), str(r["card_id"]),
                        str(r["analysis_hash"]))

    def _reload(self) -> None:
        by_venue: dict[str, Approved] = {}
        rejected = 0
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    rejected += 1
                    continue
                ok = self._valid(row) if isinstance(row, dict) else None
                if ok is None:
                    rejected += 1
                else:
                    by_venue[ok.venue] = ok       # later verified rows supersede earlier ones
        self._by_venue, self.rejected_rows = by_venue, rejected

    def approved(self, venue: str) -> Approved | None:
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime if self.path.exists() else None
            except OSError:
                mtime = None
            if mtime != self._mtime:
                self._reload()
                self._mtime = mtime
            return self._by_venue.get(venue.lower())


class BasketGate:
    """The Router's view of the book for one venue."""

    def __init__(self, venue: str, book: BasketBook) -> None:
        self.venue, self.book = venue.lower(), book

    def check_entry(self, symbol: str) -> str | None:
        """None if the entry is inside the signed basket, else the rejection reason."""
        ap = self.book.approved(self.venue)
        if ap is None:
            return f"no card-signed basket for venue {self.venue}"
        if symbol.strip().upper() not in ap.symbols:
            return f"{symbol} is outside the card-signed {self.venue} basket"
        return None


def registry_verifier(db_path: Path) -> Callable[[str, bytes, bytes], bool]:
    """Verify against the paired cards in ``approval.db``, re-read on every use.

    A card paired with the shim after this process started is in the database but not in any
    already-open registry's cache, so a fresh registry is opened per verification. Baskets are
    reloaded only when the file changes, so this is not on the trading path.
    """
    def verify(card_id: str, canonical: bytes, signature: bytes) -> bool:
        from .approval_sqlite import SqliteCardRegistry
        with SqliteCardRegistry(db_path) as reg:
            return bool(reg.verify(card_id, canonical, signature))
    return verify


def attach_basket_gate(router: Any, venue: str, state_dir: Path, *, enabled: bool) -> BasketGate | None:
    """Put the basket gate on ``router`` (the inner Router, before any card wrapper). No-op if off."""
    if not enabled:
        return None
    book = BasketBook(Path(state_dir) / "baskets.jsonl", registry_verifier(Path(state_dir) / "approval.db"))
    router.basket_gate = BasketGate(venue, book)
    return router.basket_gate
