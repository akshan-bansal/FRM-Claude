"""SQLite read model projected from the ledger (``AUDIT_LEDGER_SCOPE.md`` phase 7).

The ledger is the write path and the source of truth. This is a **derived, disposable** view of it,
rebuilt from scratch by replaying ``state/ledger/*.jsonl``. Nothing here is ever the authority: if
the projection and the ledger disagree, the ledger wins and the disagreement is itself the finding.
Delete the file and rebuild whenever you doubt it.

Three tables:

* ``ledger_events`` — the flat spine, one row per ledger row, keyed ``(stream, seq)`` so a replay is
  idempotent and a partial rebuild can resume.
* ``intents`` — one row per intent, folded from its events: what was signalled, what the gate said,
  what the card said, what the broker did, and whether it reached a terminal state.
* ``chain_status`` — one row per stream recording the last verification, so "is the record intact"
  is queryable rather than only printable.

Deliberately NOT on the trading path: building this walks every ledger file and does a lot of small
writes. Run it from ``scripts/rebuild_projection.py`` in its own process, never inside a poll loop.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from .ledger import read_stream, verify_chain

log = get_logger(__name__)

PROJECTION_SCHEMA_VERSION = "1.0.0"

# Events that end an intent's life. An intent with none of these and a BROKER_SUBMITTED is the
# crashed-writer case worth surfacing.
TERMINAL_EVENTS = frozenset({"FILLED", "PARTIAL", "CANCELLED", "BROKER_REJECTED", "RISK_REJECTED",
                             "REJECTED", "EXPIRED", "SIGNAL_SUPPRESSED"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_events (
    stream             TEXT NOT NULL,
    seq                INTEGER NOT NULL,
    ts                 TEXT NOT NULL,
    event              TEXT NOT NULL,
    mode               TEXT,
    intent_id          TEXT,
    session_id         TEXT,
    user_id            TEXT,
    device_id          TEXT,
    strategy_id        TEXT,
    strategy_version   TEXT,
    risk_check_version TEXT,
    broker_order_id    TEXT,
    signature          TEXT,
    signing_key_id     TEXT,
    payload            TEXT NOT NULL,      -- raw JSON; the projection does not flatten every event
    row_hash           TEXT NOT NULL,
    PRIMARY KEY (stream, seq)
);

CREATE INDEX IF NOT EXISTS idx_events_intent ON ledger_events(intent_id);
CREATE INDEX IF NOT EXISTS idx_events_event  ON ledger_events(event);
CREATE INDEX IF NOT EXISTS idx_events_ts     ON ledger_events(ts);

CREATE TABLE IF NOT EXISTS intents (
    intent_id          TEXT PRIMARY KEY,
    stream             TEXT,
    first_seen         TEXT,
    last_seen          TEXT,
    symbol             TEXT,
    action             TEXT,
    requested_shares   REAL,
    filled_shares      REAL,
    entry              REAL,
    strategy_id        TEXT,
    strategy_version   TEXT,
    risk_check_version TEXT,
    gate_accepted      INTEGER,
    rejected_reasons   TEXT,
    trimmed_from       REAL,
    queued             INTEGER NOT NULL DEFAULT 0,
    sent_at            TEXT,
    verdict            TEXT,
    verdict_at         TEXT,
    signer_card_id     TEXT,             -- from the verdict row's payload
    signing_key_id     TEXT,             -- from the SIGNED row's envelope; should agree, and a
                                         -- disagreement between the two is itself a finding
    signature          TEXT,
    canonical          TEXT,
    fingerprint        TEXT,
    broker_order_id    TEXT,
    terminal_event     TEXT,
    outcome            TEXT,               -- 'filled' | 'partial' | 'rejected' | 'expired' | 'open'
    event_count        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_intents_symbol  ON intents(symbol);
CREATE INDEX IF NOT EXISTS idx_intents_outcome ON intents(outcome);
CREATE INDEX IF NOT EXISTS idx_intents_version ON intents(strategy_version);

CREATE TABLE IF NOT EXISTS chain_status (
    stream        TEXT PRIMARY KEY,
    rows          INTEGER NOT NULL,
    last_seq      INTEGER,
    last_row_hash TEXT,
    verified_at   TEXT NOT NULL,
    ok            INTEGER NOT NULL,
    reason        TEXT NOT NULL
);
"""


def open_db(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                 (PROJECTION_SCHEMA_VERSION,))
    return conn


def streams(ledger_dir: Path) -> dict[str, list[Path]]:
    """``{stream: [day files, oldest first]}`` from ``<date>.<stream>.jsonl`` names."""
    out: dict[str, list[Path]] = {}
    for path in sorted(Path(ledger_dir).glob("*.jsonl")):
        parts = path.name.split(".")
        if len(parts) < 3:
            continue
        out.setdefault(".".join(parts[1:-1]), []).append(path)
    return out


def _rows_of(paths: list[Path]) -> Iterator[dict[str, Any]]:
    for path in paths:
        yield from read_stream(path)


def _as_text(value: Any) -> str | None:
    return None if value is None else str(value)


def rebuild(ledger_dir: Path | str, db_path: Path | str) -> dict[str, Any]:
    """Replay every stream into a fresh projection. Returns a summary dict.

    Drops and repopulates the derived tables rather than patching them: a projection that can only
    be migrated forward eventually disagrees with the ledger in ways nobody can explain, and this
    is cheap to rebuild by construction.
    """
    ledger_dir = Path(ledger_dir)
    conn = open_db(db_path)
    from datetime import UTC, datetime
    now = datetime.now(UTC).isoformat()
    with conn:
        conn.execute("DELETE FROM ledger_events")
        conn.execute("DELETE FROM intents")
        conn.execute("DELETE FROM chain_status")

    found = streams(ledger_dir)
    summary: dict[str, Any] = {"streams": {}, "events": 0, "intents": 0, "chains_ok": True}
    folded: dict[str, dict[str, Any]] = {}

    for stream, paths in sorted(found.items()):
        rows = list(_rows_of(paths))
        ok, reason = verify_chain(rows)
        summary["chains_ok"] = summary["chains_ok"] and ok
        with conn:
            for row in rows:
                conn.execute(
                    "INSERT OR REPLACE INTO ledger_events(stream, seq, ts, event, mode, intent_id,"
                    " session_id, user_id, device_id, strategy_id, strategy_version,"
                    " risk_check_version, broker_order_id, signature, signing_key_id, payload,"
                    " row_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (stream, row.get("seq"), row.get("ts"), row.get("event"), row.get("mode"),
                     row.get("intent_id"), row.get("session_id"), row.get("user_id"),
                     row.get("device_id"), row.get("strategy_id"), row.get("strategy_version"),
                     row.get("risk_check_version"), _as_text(row.get("broker_order_id")),
                     row.get("signature"), row.get("signing_key_id"),
                     json.dumps(row.get("payload") or {}, sort_keys=True), row.get("row_hash", "")),
                )
                _fold(folded, stream, row)
            conn.execute(
                "INSERT OR REPLACE INTO chain_status(stream, rows, last_seq, last_row_hash,"
                " verified_at, ok, reason) VALUES (?,?,?,?,?,?,?)",
                (stream, len(rows), rows[-1].get("seq") if rows else None,
                 rows[-1].get("row_hash") if rows else None, now, int(ok), reason),
            )
        summary["streams"][stream] = {"rows": len(rows), "files": len(paths), "ok": ok,
                                      "reason": reason}
        summary["events"] += len(rows)
        if not ok:
            log.error("projection.chain_broken", stream=stream, reason=reason)

    with conn:
        for intent in folded.values():
            cols = ", ".join(intent)
            marks = ", ".join("?" for _ in intent)
            conn.execute(f"INSERT OR REPLACE INTO intents({cols}) VALUES ({marks})",
                         tuple(intent.values()))
    summary["intents"] = len(folded)
    conn.close()
    log.info("projection.rebuilt", **{k: v for k, v in summary.items() if k != "streams"})
    return summary


def _fold(folded: dict[str, dict[str, Any]], stream: str, row: dict[str, Any]) -> None:
    """Accumulate one ledger row into its intent's folded state.

    Events without an intent_id (POSITION_FLATTENED, a bare signal) stay in ``ledger_events`` only:
    inventing a synthetic intent for them would put rows in ``intents`` that no intent produced.
    """
    intent_id = row.get("intent_id")
    if not intent_id:
        return
    cur = folded.setdefault(str(intent_id), {
        "intent_id": str(intent_id), "stream": stream, "first_seen": row.get("ts"),
        "queued": 0, "event_count": 0, "outcome": "open",
    })
    payload = row.get("payload") or {}
    event = str(row.get("event"))
    cur["last_seen"] = row.get("ts")
    cur["event_count"] = int(cur.get("event_count", 0)) + 1

    for key, src in (("strategy_id", "strategy_id"), ("strategy_version", "strategy_version"),
                     ("risk_check_version", "risk_check_version")):
        if row.get(src):
            cur[key] = row.get(src)
    for key in ("symbol", "action", "entry"):
        if payload.get(key) is not None and cur.get(key) is None:
            cur[key] = payload.get(key)
    if payload.get("fingerprint"):
        cur["fingerprint"] = payload["fingerprint"]

    if event in ("RISK_CHECK", "RISK_REJECTED"):
        cur["gate_accepted"] = int(bool(payload.get("accepted")))
        reasons = payload.get("rejected_reasons") or []
        if reasons:
            cur["rejected_reasons"] = json.dumps(reasons)
        if payload.get("shares") is not None:
            cur["requested_shares"] = payload["shares"]
    elif event == "RISK_TRIMMED":
        cur["trimmed_from"] = payload.get("from_shares")
        cur["requested_shares"] = payload.get("to_shares")
    elif event == "INTENT_QUEUED":
        cur["queued"] = 1
    elif event == "INTENT_SENT":
        cur["sent_at"] = row.get("ts")
    elif event in ("APPROVED", "REJECTED", "EXPIRED"):
        cur["verdict"] = payload.get("verdict") or event
        cur["verdict_at"] = row.get("ts")
        if payload.get("signer_card_id"):
            cur["signer_card_id"] = payload["signer_card_id"]
    elif event == "SIGNED":
        cur["signature"] = row.get("signature")
        cur["signing_key_id"] = row.get("signing_key_id")
        cur["canonical"] = payload.get("canonical")
    elif event in ("FILLED", "PARTIAL"):
        cur["filled_shares"] = payload.get("shares")
        cur["broker_order_id"] = _as_text(row.get("broker_order_id"))
        if payload.get("requested_shares") is not None:
            cur["requested_shares"] = payload["requested_shares"]

    if event in TERMINAL_EVENTS:
        cur["terminal_event"] = event
        cur["outcome"] = {
            "FILLED": "filled", "PARTIAL": "partial", "CANCELLED": "cancelled",
            "BROKER_REJECTED": "rejected", "RISK_REJECTED": "rejected", "REJECTED": "declined",
            "EXPIRED": "expired", "SIGNAL_SUPPRESSED": "suppressed",
        }.get(event, "open")
    # `signing_key_id` arrives on the SIGNED row; keep the column present for every intent so the
    # INSERT column list is stable across folds.
    cur.setdefault("signing_key_id", None)


# --------------------------------------------------------------------------- #
# the questions from scope 2.5 that only a query can answer                    #
# --------------------------------------------------------------------------- #

def rejection_reasons(conn: sqlite3.Connection, *, limit: int = 20) -> list[tuple[str, int]]:
    """Most common gate-rejection reasons. Reasons are stored as a JSON list, so unpack in Python
    rather than pretending SQLite can split them."""
    from collections import Counter
    counter: Counter[str] = Counter()
    for (blob,) in conn.execute(
            "SELECT rejected_reasons FROM intents WHERE rejected_reasons IS NOT NULL"):
        for reason in json.loads(blob):
            counter[str(reason)] += 1
    return counter.most_common(limit)


def approval_latency_seconds(conn: sqlite3.Connection) -> list[float]:
    """Seconds from INTENT_SENT to the card's verdict — the TTL-pressure question."""
    from datetime import datetime
    out: list[float] = []
    for sent, decided in conn.execute(
            "SELECT sent_at, verdict_at FROM intents "
            "WHERE sent_at IS NOT NULL AND verdict_at IS NOT NULL"):
        out.append((datetime.fromisoformat(decided) - datetime.fromisoformat(sent))
                   .total_seconds())
    return out


def versions_traded(conn: sqlite3.Connection, symbol: str) -> list[tuple[str, str, int]]:
    """``(strategy_id, strategy_version, fills)`` for one symbol — what actually traded it."""
    return list(conn.execute(
        "SELECT COALESCE(strategy_id,'?'), COALESCE(strategy_version,'unversioned'), COUNT(*) "
        "FROM intents WHERE symbol = ? AND outcome IN ('filled','partial') "
        "GROUP BY 1, 2 ORDER BY 3 DESC", (symbol,)))


def dangling_submissions(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Intents that reached the broker and never reached a terminal event.

    The crashed-writer case: the order may well have filled, and the record simply stops. Worth
    surfacing loudly rather than counting as "open".
    """
    return list(conn.execute(
        "SELECT i.intent_id, COALESCE(i.symbol,'?'), i.last_seen FROM intents i "
        "WHERE i.terminal_event IS NULL AND EXISTS ("
        "  SELECT 1 FROM ledger_events e WHERE e.intent_id = i.intent_id "
        "  AND e.event = 'BROKER_SUBMITTED') ORDER BY i.last_seen"))


def signed_intents(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    """``(intent_id, signing_key_id, signature, canonical)`` for every intent with a signature."""
    return list(conn.execute(
        "SELECT intent_id, COALESCE(signing_key_id,''), signature, COALESCE(canonical,'') "
        "FROM intents WHERE signature IS NOT NULL"))
