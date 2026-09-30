"""Card-approval layer that wraps :class:`Router`.

Convenience helper :func:`wire_card_approval` builds the store, registry, VS
engine and background shim thread in one shot — used by the paper-trading
scripts under the ``--require-card`` flag.


An ``ApprovalRouter`` publishes each risk-passed intent to an
:class:`ApprovalStore`, waits for a card-signed ACCEPT / DECLINE / expiry,
and only then forwards the intent to the underlying router.

The engine here is the server-side counterpart of an ESP32-hosted approval
card. The ESP32:

  1. long-polls (or subscribes over BLE via a phone bridge) for pending
     prompts,
  2. renders the compact prompt on its e-ink display,
  3. reads a user tap on ACCEPT / DECLINE,
  4. signs the intent's ``canonical`` byte string with an Ed25519 key held
     in its secure element,
  5. POSTs the signed response back.

The router NEVER trusts a response whose signature does not verify against
the pubkey the card registered at pairing time, and never trusts a
response for an intent that has expired or already been consumed.
"""
from __future__ import annotations

import secrets
import threading
import time
from base64 import b64decode
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from .approval_sqlite import SqliteApprovalStore, SqliteCardRegistry

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from ..brokers.models import Order, plain_decimal
from ..logging_setup import get_logger
from .router import OrderIntent, Router

log = get_logger(__name__)

Verdict = Literal["ACCEPT", "DECLINE", "EXPIRED"]


# --------------------------------------------------------------------------- #
# canonical string                                                            #
# --------------------------------------------------------------------------- #

def canonical_bytes(
    *,
    broker: str,
    action: str,
    symbol: str,
    shares: float,
    entry: float,
    notional_usd: float,
    account: str,
    intent_id: str,
    nonce: str,
) -> bytes:
    """Bytes the card must sign. WYSIWYS: every field a user cares about is in here.

    ``broker`` is included so a MITM cannot silently redirect an approved
    intent from (say) IB to Kraken and back — the destination brokerage is
    part of what the user is approving.
    """
    parts = [
        broker,
        action,
        symbol,
        # Whole quantities keep the historical "12" form, so deployed-card
        # signatures are unchanged; fractional crypto must not truncate to 0.
        plain_decimal(shares),
        f"{float(entry):.4f}",
        f"{float(notional_usd):.2f}",
        account,
        intent_id,
        nonce,
    ]
    return "|".join(parts).encode("utf-8")


def fingerprint(canonical: bytes, *, chars: int = 8) -> str:
    """Human-comparable digest of the exact bytes signed: ``7F3A...91C2``.

    ``chars`` is the total number of hex characters shown, split evenly between head and tail of
    the SHA-256 digest. For eyeballing that the dashboard, the device screen and the ledger all
    refer to the same intent. Verification always uses the full signature over the full canonical
    bytes — never this abbreviation.

    A caller MUST derive it from the same stored record the card was served, not by recomputing the
    fields from a separate source, or the display and the signature can drift apart and the
    comparison becomes theatre.
    """
    if chars < 2 or chars % 2:
        raise ValueError(f"chars must be a positive even number, got {chars}")
    digest = sha256(canonical).hexdigest().upper()
    half = chars // 2
    return f"{digest[:half]}...{digest[-half:]}"


def verify_audit_record(record: Mapping[str, object]) -> tuple[bool, str]:
    """Re-verify one stored approval offline. Returns ``(ok, reason)``.

    Takes the dict from ``SqliteApprovalStore.audit_record`` and needs nothing else — no shim, no
    registry, no network. ``ok`` is False with a reason when the evidence is absent (an EXPIRED
    intent, or an approval recorded before signatures were stored) as well as when it is wrong, so
    a caller can never read "no signature on file" as "verified".
    """
    verdict = record.get("verdict")
    if verdict in (None, "EXPIRED"):
        return False, f"nothing signed it (verdict={verdict})"
    sig_b64, canonical, pem = record.get("signature"), record.get("canonical"), record.get("pubkey_pem")
    if not sig_b64:
        return False, "no signature on file (approval predates signature storage)"
    if not canonical:
        return False, "no canonical bytes on file"
    if not pem:
        return False, f"signing card {record.get('signer_card_id')!r} has no public key on file"
    alg = record.get("signature_alg") or "ed25519"
    if alg != "ed25519":
        return False, f"unsupported signature algorithm {alg!r}"
    try:
        key = load_pem_public_key(str(pem).encode("utf-8"))
        if not isinstance(key, Ed25519PublicKey):
            return False, "stored key is not Ed25519"
        key.verify(b64decode(str(sig_b64)), str(canonical).encode("utf-8"))
    except InvalidSignature:
        return False, "signature does not verify against the canonical bytes"
    except Exception as e:                                    # malformed key/base64 on disk
        return False, f"unverifiable record: {type(e).__name__}: {e}"
    return True, "ok"


# --------------------------------------------------------------------------- #
# data classes                                                                #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Prompt:
    """The payload rendered on the card and delivered by ``GET /intents/pending``."""

    intent_id: str
    issued_at: datetime
    expires_at: datetime
    broker: str            # "ib" | "kraken" | "questrade" — destination brokerage
    symbol: str
    action: str
    shares: float
    entry: float
    stop: float
    target: float | None
    notional_usd: float
    risk_dollars: float
    strategy: str
    account: str
    mode: str
    thesis: str            # short natural-language reason for the trade (<=140 chars)
    intel_ref: str         # opaque id for full VS investment-engine writeup
    nonce: str
    canonical: str

    @property
    def fingerprint(self) -> str:
        """``7F3A...91C2`` over the exact bytes the card signs (see ``fingerprint()``)."""
        return fingerprint(self.canonical.encode("utf-8"))

    def to_dict(self) -> dict[str, object]:
        return {
            "intent_id": self.intent_id,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "broker": self.broker,
            "symbol": self.symbol,
            "action": self.action,
            "shares": self.shares,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "notional_usd": self.notional_usd,
            "risk_dollars": self.risk_dollars,
            "strategy": self.strategy,
            "account": self.account,
            "mode": self.mode,
            "thesis": self.thesis,
            "intel_ref": self.intel_ref,
            "nonce": self.nonce,
            "canonical": self.canonical,
            # Phase 6: the comparison value shown on the dashboard, the device and the ledger.
            # Derived from the canonical bytes in THIS record, never recomputed from the fields
            # separately, so what is displayed cannot drift from what is signed.
            "fingerprint": self.fingerprint,
        }


@dataclass
class _PendingEntry:
    prompt: Prompt
    event: threading.Event = field(default_factory=threading.Event)
    verdict: Verdict | None = None
    consumed: bool = False


@dataclass(frozen=True)
class PassbookEntry:
    """A resolved prompt — mirrors what the on-device passbook stores, but
    server-side and with more detail (thesis, notional, signer's card_id)."""

    intent_id: str
    resolved_at: datetime
    verdict: Verdict
    broker: str
    symbol: str
    action: str
    shares: float
    notional_usd: float
    strategy: str
    thesis: str
    intel_ref: str
    card_id: str | None       # who signed the ACCEPT / DECLINE; None for EXPIRED
    # Phase 6: the fingerprint of the canonical bytes this verdict covered. Empty only for a row
    # recorded before the canonical string was kept alongside the passbook entry.
    fingerprint: str = ""
    # When the prompt was put in front of the card. Carried so response time is computable from a
    # passbook row alone: without it a reader can only see when a verdict landed, not how long the
    # holder took, and the metric that needed it was returning a constant instead.
    issued_at: datetime | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "intent_id": self.intent_id,
            "resolved_at": self.resolved_at.isoformat(),
            "verdict": self.verdict,
            "broker": self.broker,
            "symbol": self.symbol,
            "action": self.action,
            "shares": self.shares,
            "notional_usd": self.notional_usd,
            "strategy": self.strategy,
            "thesis": self.thesis,
            "intel_ref": self.intel_ref,
            "card_id": self.card_id,
            "fingerprint": self.fingerprint,
        }


# --------------------------------------------------------------------------- #
# signature verification                                                      #
# --------------------------------------------------------------------------- #

class CardRegistry:
    """Maps card_id → Ed25519 public key. Populated at pairing time."""

    def __init__(self) -> None:
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._lock = threading.Lock()

    def register(self, card_id: str, pubkey_pem: bytes) -> None:
        key = load_pem_public_key(pubkey_pem)
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("card pubkey must be Ed25519")
        with self._lock:
            self._keys[card_id] = key
        log.info("approval.card_registered", card_id=card_id)

    def revoke(self, card_id: str) -> bool:
        """Remove a card's pubkey. Returns True if it was registered."""
        with self._lock:
            existed = self._keys.pop(card_id, None) is not None
        if existed:
            log.info("approval.card_revoked", card_id=card_id)
        return existed

    def card_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._keys.keys())

    def verify(self, card_id: str, canonical: bytes, signature: bytes) -> bool:
        with self._lock:
            key = self._keys.get(card_id)
        if key is None:
            log.warning("approval.unknown_card", card_id=card_id)
            return False
        try:
            key.verify(signature, canonical)
            return True
        except InvalidSignature:
            log.warning("approval.bad_signature", card_id=card_id)
            return False


# --------------------------------------------------------------------------- #
# store                                                                       #
# --------------------------------------------------------------------------- #

class ApprovalStore(Protocol):
    def publish(
        self,
        intent: OrderIntent,
        *,
        mode: str,
        broker: str,
        ttl_seconds: float,
        thesis: str = ...,
        intel_ref: str = ...,
    ) -> Prompt: ...
    def wait(self, intent_id: str, *, timeout: float | None = None) -> Verdict: ...
    def pending(self) -> list[Prompt]: ...
    def respond(
        self,
        intent_id: str,
        *,
        decision: Literal["ACCEPT", "DECLINE"],
        card_id: str,
        signature: bytes,
    ) -> bool: ...
    def passbook(self, *, limit: int = 50, offset: int = 0) -> list[PassbookEntry]: ...


class InMemoryApprovalStore:
    """Thread-safe, single-process store. Fine for a laptop shim + one card.

    Keeps a bounded FIFO ``passbook`` of resolved prompts (ACCEPT / DECLINE
    / EXPIRED) capped at ``passbook_max`` entries. Exposed by the shim as
    ``GET /passbook`` for a companion dashboard or an off-card history view.
    """

    def __init__(self, registry: CardRegistry, *, passbook_max: int = 500) -> None:
        self._registry = registry
        self._entries: dict[str, _PendingEntry] = {}
        self._lock = threading.Lock()
        self._passbook: deque[PassbookEntry] = deque(maxlen=passbook_max)

    # -- helpers ---------------------------------------------------------- #

    @staticmethod
    def _mint_intent_id() -> str:
        # Time-prefixed hex so pending lists sort naturally on the card.
        return f"{int(time.time_ns()):016x}-{secrets.token_hex(6)}"

    def _sweep_expired(self, now: datetime) -> None:
        for entry in list(self._entries.values()):
            if entry.verdict is None and entry.prompt.expires_at <= now:
                entry.verdict = "EXPIRED"
                self._passbook.append(self._entry_to_passbook(entry, card_id=None, at=now))
                entry.event.set()

    @staticmethod
    def _entry_to_passbook(
        entry: _PendingEntry, *, card_id: str | None, at: datetime,
    ) -> PassbookEntry:
        p = entry.prompt
        assert entry.verdict is not None
        return PassbookEntry(
            intent_id=p.intent_id,
            resolved_at=at,
            verdict=entry.verdict,
            broker=p.broker,
            symbol=p.symbol,
            action=p.action,
            shares=p.shares,
            notional_usd=p.notional_usd,
            strategy=p.strategy,
            thesis=p.thesis,
            intel_ref=p.intel_ref,
            card_id=card_id,
            fingerprint=p.fingerprint,
            issued_at=p.issued_at,
        )

    # -- ApprovalStore ---------------------------------------------------- #

    def publish(
        self,
        intent: OrderIntent,
        *,
        mode: str,
        broker: str,
        ttl_seconds: float,
        thesis: str = "",
        intel_ref: str = "",
    ) -> Prompt:
        now = datetime.now(UTC)
        # Reuse the id the intent was born with (2026-09-24) so the approval record and
        # the router's journals share one key; fall back for a caller passing a bare
        # intent-shaped object without one.
        intent_id = getattr(intent, "intent_id", "") or self._mint_intent_id()
        nonce = secrets.token_hex(16)
        notional = float(intent.shares) * float(intent.entry)
        canonical = canonical_bytes(
            broker=broker,
            action=intent.action.value,
            symbol=intent.symbol,
            shares=intent.shares,
            entry=intent.entry,
            notional_usd=notional,
            account=intent.account_number,
            intent_id=intent_id,
            nonce=nonce,
        ).decode("utf-8")
        prompt = Prompt(
            intent_id=intent_id,
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            broker=broker,
            symbol=intent.symbol,
            action=intent.action.value,
            shares=intent.shares,
            entry=intent.entry,
            stop=intent.stop,
            target=intent.target,
            notional_usd=notional,
            risk_dollars=intent.risk_dollars,
            strategy=intent.strategy,
            account=intent.account_number,
            mode=mode,
            thesis=thesis[:140],
            intel_ref=intel_ref,
            nonce=nonce,
            canonical=canonical,
        )
        with self._lock:
            self._sweep_expired(now)
            self._entries[intent_id] = _PendingEntry(prompt=prompt)
        log.info("approval.published", intent_id=intent_id, symbol=intent.symbol,
                 shares=intent.shares, ttl=ttl_seconds)
        return prompt

    def wait(self, intent_id: str, *, timeout: float | None = None) -> Verdict:
        with self._lock:
            entry = self._entries.get(intent_id)
        if entry is None:
            raise KeyError(intent_id)

        remaining = (entry.prompt.expires_at - datetime.now(UTC)).total_seconds()
        wait_for = remaining if timeout is None else min(remaining, timeout)
        wait_for = max(wait_for, 0.0)
        entry.event.wait(timeout=wait_for)

        with self._lock:
            if entry.verdict is None:
                # Timed out; treat as expired if the prompt is past its window.
                if entry.prompt.expires_at <= datetime.now(UTC):
                    entry.verdict = "EXPIRED"
                else:
                    # Caller's own timeout, prompt still live — surface as EXPIRED
                    # to the router (it will journal and drop the intent).
                    entry.verdict = "EXPIRED"
                entry.event.set()
            return entry.verdict

    def pending(self) -> list[Prompt]:
        now = datetime.now(UTC)
        with self._lock:
            self._sweep_expired(now)
            return [e.prompt for e in self._entries.values() if e.verdict is None]

    def respond(
        self,
        intent_id: str,
        *,
        decision: Literal["ACCEPT", "DECLINE"],
        card_id: str,
        signature: bytes,
    ) -> bool:
        with self._lock:
            entry = self._entries.get(intent_id)
            if entry is None:
                log.warning("approval.response_unknown_intent", intent_id=intent_id)
                return False
            if entry.consumed or entry.verdict is not None:
                log.warning("approval.response_replay", intent_id=intent_id,
                            prior_verdict=entry.verdict)
                return False
            if entry.prompt.expires_at <= datetime.now(UTC):
                entry.verdict = "EXPIRED"
                entry.event.set()
                return False

        canonical = entry.prompt.canonical.encode("utf-8")
        if not self._registry.verify(card_id, canonical, signature):
            # Do NOT consume the entry; a genuine card can still respond.
            return False

        with self._lock:
            # Re-check: the lock was released during verify, so a concurrent
            # response may have resolved this prompt in the meantime.
            if entry.consumed or entry.verdict is not None:
                log.warning("approval.response_replay", intent_id=intent_id,
                            prior_verdict=entry.verdict)
                return False
            entry.consumed = True
            entry.verdict = decision
            self._passbook.append(
                self._entry_to_passbook(entry, card_id=card_id, at=datetime.now(UTC))
            )
            entry.event.set()
        log.info("approval.response", intent_id=intent_id, decision=decision, card_id=card_id)
        return True

    def passbook(self, *, limit: int = 50, offset: int = 0) -> list[PassbookEntry]:
        """Newest-first view of resolved prompts. ``limit`` capped at 500."""
        if limit <= 0:
            return []
        limit = min(limit, 500)
        offset = max(offset, 0)
        with self._lock:
            snapshot = list(self._passbook)
        # Newest-first
        snapshot.reverse()
        return snapshot[offset : offset + limit]


# --------------------------------------------------------------------------- #
# router wrapper                                                              #
# --------------------------------------------------------------------------- #

ThesisFn = "Callable[[OrderIntent, str], tuple[str, str]]"  # (intent, broker) -> (thesis, intel_ref)


class ApprovalRouter:
    """Wraps a :class:`Router` and requires a card-signed ACCEPT before dispatch.

    Ordering matters: risk gates run FIRST. We never prompt the user for
    something the router would reject anyway.

    If ``thesis_fn`` is supplied, it is called after the gates pass and
    before the prompt is published. It should return ``(thesis, intel_ref)``
    — see :mod:`trading_live_claude.intel.vs_engine` for the default
    implementation. Any exception raised by ``thesis_fn`` is swallowed and
    the prompt is published with empty intel fields; the trading loop
    should not fail because the narrator is unavailable.
    """

    def __init__(
        self,
        inner: Router,
        *,
        store: ApprovalStore,
        ttl_seconds: float = 90.0,
        thesis_fn: object | None = None,
    ) -> None:
        if inner.mode == "autonomous":
            # Autonomous == no human loop. Card approval is incompatible.
            raise ValueError(
                "ApprovalRouter cannot wrap an autonomous Router; pick one loop."
            )
        self.inner = inner
        self.store = store
        self.ttl_seconds = ttl_seconds
        self.thesis_fn = thesis_fn

    def _ledger(self, event: str, intent: OrderIntent, payload: dict[str, object]) -> None:
        """Mirror an approval-axis transition into the inner router's ledger, if it has one."""
        ledger = getattr(self.inner, "ledger", None)
        if ledger is None:
            return
        ledger.append(event, payload, intent_id=getattr(intent, "intent_id", None),
                      strategy_id=intent.strategy,
                      strategy_version=getattr(intent, "strategy_version", "") or None,
                      risk_check_version=self.inner.risk_check_version())

    def _ledger_verdict(self, intent: OrderIntent, prompt: Prompt, verdict: str) -> None:
        """Record the card's verdict, and SIGNED with the signature when one exists.

        An EXPIRED prompt was never signed, so only the verdict is recorded — writing a SIGNED row
        with an empty signature would imply evidence that does not exist.
        """
        ledger = getattr(self.inner, "ledger", None)
        if ledger is None:
            return
        event = {"ACCEPT": "APPROVED", "DECLINE": "REJECTED"}.get(verdict, "EXPIRED")
        rec: dict[str, object] = {}
        audit = getattr(self.store, "audit_record", None)
        if callable(audit):
            try:
                rec = audit(prompt.intent_id) or {}
            except Exception as e:                       # a reporting read must never break routing
                log.warning("approval_router.audit_read_failed",
                            intent_id=prompt.intent_id, error=str(e))
        self._ledger(event, intent, {
            "symbol": intent.symbol, "verdict": verdict, "broker": prompt.broker,
            "shares": prompt.shares, "notional_usd": prompt.notional_usd,
            "fingerprint": fingerprint(prompt.canonical.encode("utf-8")),
            "signer_card_id": rec.get("signer_card_id"),
        })
        signature = rec.get("signature")
        if signature:
            ledger.append("SIGNED", {
                "symbol": intent.symbol,
                "canonical": prompt.canonical,
                "fingerprint": fingerprint(prompt.canonical.encode("utf-8")),
                "signature_alg": rec.get("signature_alg"),
            }, intent_id=intent.intent_id, signature=str(signature),
                signing_key_id=str(rec.get("signer_card_id") or ""),
                strategy_id=intent.strategy,
                risk_check_version=self.inner.risk_check_version())

    def submit(
        self,
        intent: OrderIntent,
        *,
        equity: float,
        existing_risk: float,
        open_positions: int,
    ) -> Order | None:
        # The wrapper is where this intent first meets a router, so the creation event belongs here;
        # the inner router's own call is then a no-op for it (see Router.announce_intent).
        self.inner.announce_intent(intent)
        decision = self.inner._gate(
            intent,
            equity=equity,
            existing_risk=existing_risk,
            open_positions=open_positions,
        )
        if not decision.accepted:
            self.inner.journal.order_intent(
                {
                    "mode": self.inner.mode,
                    "strategy": intent.strategy,
                    "symbol": intent.symbol,
                    "action": intent.action.value,
                    "shares": intent.shares,
                    "entry": intent.entry,
                    "stop": intent.stop,
                    "target": intent.target,
                    "risk_dollars": intent.risk_dollars,
                    "accepted": False,
                    "rejected_reasons": decision.rejected_reasons,
                    "via": "approval-router",
                }
            )
            self.inner.journal.rejected(
                {"symbol": intent.symbol, "reasons": decision.rejected_reasons}
            )
            self._ledger("RISK_REJECTED", intent, {
                "symbol": intent.symbol, "accepted": False,
                "rejected_reasons": decision.rejected_reasons, "via": "approval-router",
            })
            log.warning("approval_router.rejected_pre_prompt",
                        symbol=intent.symbol, reasons=decision.rejected_reasons)
            return None

        # The gate ran and accepted: record it, or the card path's chain would begin at
        # INTENT_SENT with no evidence the risk check happened at all.
        self._ledger("RISK_CHECK", intent, {
            "accepted": True, "rejected_reasons": [], "via": "approval-router",
            "symbol": intent.symbol, "action": intent.action.value, "shares": intent.shares,
            "entry": intent.entry, "stop": intent.stop, "risk_dollars": intent.risk_dollars,
            "equity": equity, "existing_risk": existing_risk, "open_positions": open_positions,
        })
        thesis, intel_ref = "", ""
        if self.thesis_fn is not None:
            try:
                thesis, intel_ref = self.thesis_fn(intent, self.inner.broker.name)  # type: ignore[operator]
            except Exception as e:  # narrator failure must not break the loop
                log.warning("approval_router.thesis_fn_failed",
                            symbol=intent.symbol, error=str(e))
        prompt = self.store.publish(
            intent,
            mode=self.inner.mode,
            # ``.venue``, not ``.name``: the wire vocabulary is venue tags (ib / ib_web / kraken /
            # questrade / paper / global). ``.name`` would send "interactive-brokers" for IBBroker
            # and "interactive-brokers-web" for IBWebBroker, neither of which the schema accepts.
            broker=getattr(self.inner.broker, "venue", None) or self.inner.broker.name,
            ttl_seconds=self.ttl_seconds,
            thesis=thesis,
            intel_ref=intel_ref,
        )
        self._ledger("INTENT_SENT", intent, {
            "symbol": intent.symbol, "action": intent.action.value, "shares": intent.shares,
            "entry": intent.entry, "notional_usd": prompt.notional_usd,
            "broker": prompt.broker, "ttl_seconds": self.ttl_seconds,
            "fingerprint": fingerprint(prompt.canonical.encode("utf-8")),
            "intel_ref": intel_ref,
        })
        verdict = self.store.wait(prompt.intent_id)
        # The verdict, and — for a signed verdict — the signature itself, so the approval can be
        # re-verified from the ledger without reaching into approval.db (audit phase 5).
        self._ledger_verdict(intent, prompt, verdict)
        if verdict != "ACCEPT":
            self.inner.journal.rejected(
                {"symbol": intent.symbol, "reasons": [f"card:{verdict.lower()}"]}
            )
            log.info("approval_router.not_accepted",
                     symbol=intent.symbol, verdict=verdict, intent_id=prompt.intent_id)
            return None

        # Re-runs gates cheaply; guards against races (heat changed while user
        # was thinking, kill-switch tripped, etc.). If it now fails, the
        # underlying router journals the rejection.
        return self.inner.submit(
            intent,
            equity=equity,
            existing_risk=existing_risk,
            open_positions=open_positions,
        )


# --------------------------------------------------------------------------- #
# convenience wiring for paper/live scripts                                   #
# --------------------------------------------------------------------------- #

@dataclass
class CardWiring:
    """The pieces the paper scripts need to hold onto when --require-card is on."""

    router: ApprovalRouter
    store: InMemoryApprovalStore
    registry: CardRegistry
    shim_thread: threading.Thread | None
    shim_url: str
    auth_token: str | None = None

    @property
    def desk_url(self) -> str:
        """Where the desk panel is reachable while this session runs.

        The shim serves the built panel itself, so the page's fetches are same-origin and
        its approvals surface reads the live store rather than the last build's journal.
        """
        return f"{self.shim_url}/desk"


def wire_card_approval(
    inner: Router,
    *,
    shim_host: str = "127.0.0.1",
    shim_port: int = 8787,
    ttl_seconds: float = 90.0,
    start_shim: bool = True,
    thesis_fn: object | None = None,
    auth_token: str | None = "auto",
    db_path: Path | None = None,
    desk_page: Path | None = None,
    state_dir: Path | None = None,
    session_id: str | None = None,
    account_currency: str = "USD",
    books: list[object] | None = None,
) -> CardWiring:
    """Build the card-approval layer around ``inner`` and (optionally) spin the shim.

    When ``start_shim`` is True, an HTTP server (from
    :mod:`.approval_server`) is launched in a daemon thread so callers get
    the wire surface for free. Pass ``start_shim=False`` for tests or when
    the store is being exposed some other way.

    ``auth_token``:
      * ``"auto"`` (default) — a fresh token is minted; callers should surface
        it so the card / simulator can be paired with it.
      * ``None`` — the shim runs open. Only safe on strict loopback.
      * any string — that literal token is used as-is.

    ``db_path`` — when set, the store and registry are SQLite-backed and
    prompts + pubkeys survive a restart. When ``None`` (default), the
    in-memory implementations are used — fine for tests and for a paper
    loop that treats every session as fresh.

    **One port, several brokers.** Every loop points ``db_path`` at the same
    ``state/approval.db``, so the store is already shared: one card approves the QT, Kraken and IB
    books alike. What collided was the HTTP surface — all three loops default to port 8787 and each
    minted its own token. Pass ``start_shim=False`` (the loops expose it as ``--card-attach``) to
    wrap the router without binding a port, and run one shim yourself
    (``scripts/approval_shim.py --db state/approval.db --book ...``). ``books`` declares which books
    a shim reports on for ``/v1/books``; it is ignored when ``start_shim`` is False, because the
    process that owns the port owns that declaration.
    """
    registry: CardRegistry | SqliteCardRegistry
    store: InMemoryApprovalStore | SqliteApprovalStore
    if db_path is not None:
        # Local import so the sqlite module isn't loaded when not asked for.
        from .approval_sqlite import SqliteApprovalStore, SqliteCardRegistry
        registry = SqliteCardRegistry(db_path)
        store = SqliteApprovalStore(registry, db_path)
    else:
        registry = CardRegistry()
        store = InMemoryApprovalStore(registry)
    router = ApprovalRouter(inner, store=store, ttl_seconds=ttl_seconds, thesis_fn=thesis_fn)

    resolved_token: str | None
    if auth_token == "auto":
        from .approval_server import mint_auth_token
        resolved_token = mint_auth_token()
    else:
        resolved_token = auth_token

    shim_thread: threading.Thread | None = None
    if start_shim:
        # Local import so ``execution.approval`` stays importable in contexts
        # (mypy runs, docs) where the HTTP layer isn't needed.
        from .approval_server import start_shim_thread
        shim_thread = start_shim_thread(
            store, registry, shim_host, shim_port, auth_token=resolved_token,
            desk_page=desk_page, state_dir=state_dir, session_id=session_id,
            account_currency=account_currency, books=books,
            journal=inner.journal, router=inner,
        )

    return CardWiring(
        router=router,
        store=store,
        registry=registry,
        shim_thread=shim_thread,
        shim_url=f"http://{shim_host}:{shim_port}",
        auth_token=resolved_token,
    )
