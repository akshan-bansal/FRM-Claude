"""SQLite-backed persistence for the approval store + card registry.

Same interfaces as :class:`~.approval.CardRegistry` and
:class:`~.approval.InMemoryApprovalStore`; different backing store. Enables:

  * Prompts survive a shim restart. Unresolved rows re-appear in
    ``pending()`` on boot; their ``threading.Event`` is recreated empty
    (nothing is waiting on it after a restart, but the card can still
    resolve them via the same wire).
  * The passbook becomes ``SELECT … ORDER BY resolved_at DESC LIMIT`` on
    an indexed column — the ``passbook_max`` cap moves from a
    ``deque(maxlen=N)`` in memory to a query ``LIMIT``.
  * Card pubkeys and revocations persist across boots.

Single-schema, single-owner. Multi-tenant (owners + per-owner bearer
tokens) is a coordinator concept and the zero-vendor-infra decision
(NEXT_SESSION.md §11) took it off the near-term plan.

Concurrency model: one ``sqlite3.Connection`` per store guarded by a
``threading.RLock``. WAL journal, ``isolation_level=None`` for
autocommit — every write is its own transaction. Fine at the scale of
one card + a handful of paper-loop polls per second; if that pressure
ever changes, revisit before scaling up.
"""
from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from ..logging_setup import get_logger
from .approval import PassbookEntry, Prompt, Verdict, canonical_bytes
from .router import OrderIntent

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# schema                                                                      #
# --------------------------------------------------------------------------- #

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    card_id     TEXT PRIMARY KEY,
    pubkey_pem  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    revoked_at  TEXT
);

CREATE TABLE IF NOT EXISTS intents (
    intent_id       TEXT PRIMARY KEY,
    issued_at       TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    resolved_at     TEXT,
    verdict         TEXT,               -- NULL = pending
    consumed        INTEGER NOT NULL DEFAULT 0,
    broker          TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    action          TEXT NOT NULL,
    shares          INTEGER NOT NULL,
    entry           REAL NOT NULL,
    stop            REAL NOT NULL,
    target          REAL,
    notional_usd    REAL NOT NULL,
    risk_dollars    REAL NOT NULL,
    strategy        TEXT NOT NULL,
    account         TEXT NOT NULL,
    mode            TEXT NOT NULL,
    thesis          TEXT NOT NULL DEFAULT '',
    intel_ref       TEXT NOT NULL DEFAULT '',
    nonce           TEXT NOT NULL,
    canonical       TEXT NOT NULL,
    signer_card_id  TEXT
);

CREATE INDEX IF NOT EXISTS idx_intents_resolved_at
    ON intents(resolved_at DESC);
CREATE INDEX IF NOT EXISTS idx_intents_pending
    ON intents(verdict) WHERE verdict IS NULL;
"""


def _open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        check_same_thread=False,   # we serialize with our own lock
        isolation_level=None,      # autocommit; every statement its own tx
        timeout=5.0,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    return conn


# --------------------------------------------------------------------------- #
# registry                                                                    #
# --------------------------------------------------------------------------- #

class SqliteCardRegistry:
    """Persistent card pubkey registry. Same interface as ``CardRegistry``."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._conn = _open_db(self.db_path)
        self._lock = threading.RLock()
        self._cache: dict[str, Ed25519PublicKey] = {}
        self._reload_cache()

    def _reload_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            rows = self._conn.execute(
                "SELECT card_id, pubkey_pem FROM cards WHERE revoked_at IS NULL"
            ).fetchall()
            for card_id, pem in rows:
                try:
                    key = load_pem_public_key(pem.encode("utf-8"))
                    if isinstance(key, Ed25519PublicKey):
                        self._cache[card_id] = key
                except Exception as e:  # pragma: no cover — corrupt row
                    log.warning("approval.sqlite.bad_pubkey_row",
                                card_id=card_id, error=str(e))

    def register(self, card_id: str, pubkey_pem: bytes) -> None:
        key = load_pem_public_key(pubkey_pem)
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("card pubkey must be Ed25519")
        now = datetime.now(UTC).isoformat()
        with self._lock:
            # Upsert — a re-register with a new pubkey replaces the old one.
            # revoked_at cleared so a previously-revoked card can be re-paired.
            self._conn.execute(
                "INSERT INTO cards(card_id, pubkey_pem, created_at, revoked_at) "
                "VALUES (?, ?, ?, NULL) "
                "ON CONFLICT(card_id) DO UPDATE SET "
                "  pubkey_pem = excluded.pubkey_pem, "
                "  revoked_at = NULL",
                (card_id, pubkey_pem.decode("utf-8"), now),
            )
            self._cache[card_id] = key
        log.info("approval.card_registered", card_id=card_id)

    def revoke(self, card_id: str) -> bool:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET revoked_at = ? "
                "WHERE card_id = ? AND revoked_at IS NULL",
                (now, card_id),
            )
            existed = cur.rowcount > 0
            self._cache.pop(card_id, None)
        if existed:
            log.info("approval.card_revoked", card_id=card_id)
        return existed

    def card_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._cache.keys())

    def verify(self, card_id: str, canonical: bytes, signature: bytes) -> bool:
        with self._lock:
            key = self._cache.get(card_id)
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

_INTENT_COLS = (
    "intent_id, issued_at, expires_at, broker, symbol, action, shares, entry, "
    "stop, target, notional_usd, risk_dollars, strategy, account, mode, thesis, "
    "intel_ref, nonce, canonical"
)


def _row_to_prompt(row: tuple) -> Prompt:
    return Prompt(
        intent_id=row[0],
        issued_at=datetime.fromisoformat(row[1]),
        expires_at=datetime.fromisoformat(row[2]),
        broker=row[3],
        symbol=row[4],
        action=row[5],
        shares=int(row[6]),
        entry=float(row[7]),
        stop=float(row[8]),
        target=(float(row[9]) if row[9] is not None else None),
        notional_usd=float(row[10]),
        risk_dollars=float(row[11]),
        strategy=row[12],
        account=row[13],
        mode=row[14],
        thesis=row[15],
        intel_ref=row[16],
        nonce=row[17],
        canonical=row[18],
    )


class SqliteApprovalStore:
    """Persistent, thread-safe. Satisfies :class:`~.approval.ApprovalStore`."""

    def __init__(
        self,
        registry: SqliteCardRegistry,
        db_path: Path,
        *,
        passbook_max: int = 500,
    ) -> None:
        self._registry = registry
        self._conn = _open_db(Path(db_path))
        self._lock = threading.RLock()
        self._events: dict[str, threading.Event] = {}
        self._passbook_max = passbook_max
        # Rehydrate: any unresolved intent from a previous run gets a fresh
        # (unset) Event so `wait()` can still be called on it. Nothing was
        # actually blocked when we crashed, so this loses no state.
        self._rehydrate_pending_events()

    def _rehydrate_pending_events(self) -> None:
        with self._lock:
            for row in self._conn.execute(
                "SELECT intent_id FROM intents WHERE verdict IS NULL"
            ):
                self._events[row[0]] = threading.Event()

    # -- helpers ---------------------------------------------------------- #

    @staticmethod
    def _mint_intent_id() -> str:
        return f"{int(time.time_ns()):016x}-{secrets.token_hex(6)}"

    def _sweep_expired(self) -> None:
        """Move any past-TTL, still-unresolved intents to EXPIRED."""
        now = datetime.now(UTC).isoformat()
        with self._lock:
            expired = self._conn.execute(
                "UPDATE intents SET verdict = 'EXPIRED', resolved_at = ? "
                "WHERE verdict IS NULL AND expires_at <= ? "
                "RETURNING intent_id",
                (now, now),
            ).fetchall()
            for (intent_id,) in expired:
                ev = self._events.pop(intent_id, None)
                if ev is not None:
                    ev.set()

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
        intent_id = self._mint_intent_id()
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
            self._sweep_expired()
            self._conn.execute(
                f"INSERT INTO intents({_INTENT_COLS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    prompt.intent_id, prompt.issued_at.isoformat(),
                    prompt.expires_at.isoformat(), prompt.broker, prompt.symbol,
                    prompt.action, prompt.shares, prompt.entry, prompt.stop,
                    prompt.target, prompt.notional_usd, prompt.risk_dollars,
                    prompt.strategy, prompt.account, prompt.mode, prompt.thesis,
                    prompt.intel_ref, prompt.nonce, prompt.canonical,
                ),
            )
            self._events[intent_id] = threading.Event()
        log.info("approval.published", intent_id=intent_id, symbol=intent.symbol,
                 shares=intent.shares, ttl=ttl_seconds)
        return prompt

    def wait(self, intent_id: str, *, timeout: float | None = None) -> Verdict:
        with self._lock:
            row = self._conn.execute(
                "SELECT verdict, expires_at FROM intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise KeyError(intent_id)
            verdict, expires_at_iso = row
            if verdict is not None:
                return verdict  # type: ignore[return-value]
            ev = self._events.get(intent_id)
            if ev is None:
                ev = threading.Event()
                self._events[intent_id] = ev

        expires_at = datetime.fromisoformat(expires_at_iso)
        remaining = (expires_at - datetime.now(UTC)).total_seconds()
        wait_for = remaining if timeout is None else min(remaining, timeout)
        wait_for = max(wait_for, 0.0)
        ev.wait(timeout=wait_for)

        with self._lock:
            row = self._conn.execute(
                "SELECT verdict FROM intents WHERE intent_id = ?", (intent_id,),
            ).fetchone()
            if row is None or row[0] is None:
                # No response before the caller's timeout. Surface as EXPIRED
                # so the router journals and drops. (Same shape as the
                # in-memory store's caller-timeout handling.)
                self._conn.execute(
                    "UPDATE intents SET verdict = 'EXPIRED', resolved_at = ? "
                    "WHERE intent_id = ? AND verdict IS NULL",
                    (datetime.now(UTC).isoformat(), intent_id),
                )
                self._events.pop(intent_id, None)
                return "EXPIRED"
            return row[0]  # type: ignore[return-value]

    def pending(self) -> list[Prompt]:
        with self._lock:
            self._sweep_expired()
            rows = self._conn.execute(
                f"SELECT {_INTENT_COLS} FROM intents WHERE verdict IS NULL "
                "ORDER BY issued_at ASC"
            ).fetchall()
        return [_row_to_prompt(r) for r in rows]

    def respond(
        self,
        intent_id: str,
        *,
        decision: Literal["ACCEPT", "DECLINE"],
        card_id: str,
        signature: bytes,
    ) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT verdict, consumed, expires_at, canonical "
                "FROM intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if row is None:
                log.warning("approval.response_unknown_intent", intent_id=intent_id)
                return False
            verdict, consumed, expires_at_iso, canonical = row
            if consumed or verdict is not None:
                log.warning("approval.response_replay", intent_id=intent_id,
                            prior_verdict=verdict)
                return False
            if datetime.fromisoformat(expires_at_iso) <= datetime.now(UTC):
                self._conn.execute(
                    "UPDATE intents SET verdict = 'EXPIRED', resolved_at = ? "
                    "WHERE intent_id = ? AND verdict IS NULL",
                    (datetime.now(UTC).isoformat(), intent_id),
                )
                ev = self._events.pop(intent_id, None)
                if ev is not None:
                    ev.set()
                return False

        if not self._registry.verify(card_id, canonical.encode("utf-8"), signature):
            # Do NOT consume; a real card can still respond within TTL.
            return False

        with self._lock:
            now = datetime.now(UTC).isoformat()
            self._conn.execute(
                "UPDATE intents SET verdict = ?, consumed = 1, "
                "resolved_at = ?, signer_card_id = ? "
                "WHERE intent_id = ? AND verdict IS NULL",
                (decision, now, card_id, intent_id),
            )
            ev = self._events.pop(intent_id, None)
            if ev is not None:
                ev.set()
        log.info("approval.response", intent_id=intent_id, decision=decision,
                 card_id=card_id)
        return True

    def passbook(self, *, limit: int = 50, offset: int = 0) -> list[PassbookEntry]:
        if limit <= 0:
            return []
        limit = min(limit, self._passbook_max)
        offset = max(offset, 0)
        with self._lock:
            rows = self._conn.execute(
                "SELECT intent_id, resolved_at, verdict, broker, symbol, action, "
                "  shares, notional_usd, strategy, thesis, intel_ref, "
                "  signer_card_id "
                "FROM intents WHERE verdict IS NOT NULL "
                "ORDER BY resolved_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [
            PassbookEntry(
                intent_id=r[0],
                resolved_at=datetime.fromisoformat(r[1]),
                verdict=r[2],
                broker=r[3],
                symbol=r[4],
                action=r[5],
                shares=int(r[6]),
                notional_usd=float(r[7]),
                strategy=r[8],
                thesis=r[9],
                intel_ref=r[10],
                card_id=r[11],
            )
            for r in rows
        ]
