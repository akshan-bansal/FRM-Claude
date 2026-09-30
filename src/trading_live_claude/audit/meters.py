"""Readings for the desk panel, taken from the journals rather than computed beside them.

Two things read this module: ``/v1/stats`` (the panel's LIVE strip) and ``/v1/meters/snapshot``
(the ATLAS tab's runtime readings). Both answer the same question — what is true of this session
right now — so both take the same path to it, and neither recomputes a figure the journal already
holds.

``state/paper_equity.csv`` is ground truth for equity, cash, positions value, peak and drawdown:
``PaperBroker`` writes a row per mark, and the repo's standing rule is that ``state/`` files are
read, not re-derived. Before 2026-09-28 the shim instead walked the Router journal's fills and
started from a hard-coded ``starting_equity = 100_000.0``, subtracting the whole cost of every BUY
from equity without ever adding the position back — so a panel attached to a live session showed
the untouched default and called it session equity.

Everything here fails closed. A session with no rows yields no readings, not zeros; a row that does
not reconcile raises rather than reporting a plausible number; a reading is emitted only when its
source, its scope and its observation time are all known.
"""
from __future__ import annotations

import csv
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_STALE_SECONDS = 900.0

# Meter ids, as defined in the atlas catalogue. Named here so the mapping from a journal column to
# a meter is stated once and can be read at a glance.
NAV = "001"
CASH = "002"
PNL = "003"
EXPOSURE = "006"
DRAWDOWN = "009"
PEAK_RATIO = "010"
INTENTS = "081"
PENDING = "084"
ACCEPTED = "085"
DECLINED = "086"
PASSBOOK = "100"
PROMPTS_WAITING = "112"
FILLS = "088"


class JournalMismatch(ValueError):
    """A journal row contradicts itself — reported rather than smoothed over."""


def _finite(value: Any, what: str) -> float:
    out = float(value)
    if not math.isfinite(out):
        raise JournalMismatch(f"{what} is not finite")
    return out


def _parse(stamp: str) -> datetime:
    parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise JournalMismatch(f"timestamp without a zone: {stamp}")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class EquitySnapshot:
    """The latest marked row for one session, as written by the broker."""

    session_id: str
    at: datetime
    equity: float
    cash: float
    positions_value: float
    realized_pnl: float
    unrealized_pnl: float
    peak_equity: float
    drawdown_pct: float          # fraction, as the journal stores it

    @property
    def max_drawdown_pct(self) -> float:
        return 100.0 * self.drawdown_pct

    @property
    def flat(self) -> bool:
        return abs(self.positions_value) <= 0.01


def read_equity(state_dir: Path, session_id: str) -> EquitySnapshot | None:
    """The newest ``paper_equity.csv`` row for ``session_id``, or None if it has none.

    None is the honest answer for a session that has not marked yet: there is no equity to report,
    and the previous behaviour of answering with the starting capital made an unmarked session
    indistinguishable from one that is exactly flat at its opening balance.
    """
    path = state_dir / "paper_equity.csv"
    if not path.exists() or not session_id:
        return None
    rows: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("session_id") == session_id]
    if not rows:
        return None
    row = max(rows, key=lambda r: _parse(r["ts"]))
    equity = _finite(row["equity"], "equity")
    cash = _finite(row["cash"], "cash")
    positions = _finite(row["positions_value"], "positions_value")
    # The identity the broker maintains. If it fails the row cannot be shown as a book.
    if abs(equity - cash - positions) > max(0.02, abs(equity) * 1e-6):
        raise JournalMismatch(
            f"equity {equity} does not reconcile with cash {cash} + positions {positions}")
    drawdown = _finite(row["drawdown_pct"], "drawdown_pct")
    if not 0.0 <= drawdown <= 1.0:
        raise JournalMismatch(f"drawdown_pct outside [0, 1]: {drawdown}")
    return EquitySnapshot(
        session_id=session_id,
        at=_parse(row["ts"]),
        equity=equity,
        cash=cash,
        positions_value=positions,
        realized_pnl=_finite(row["realized_pnl"], "realized_pnl"),
        unrealized_pnl=_finite(row["unrealized_pnl"], "unrealized_pnl"),
        peak_equity=_finite(row["peak_equity"], "peak_equity"),
        drawdown_pct=drawdown,
    )


def _reading(value: float | int | str, unit: str, source: str, as_of: datetime | None,
             scope: str, *, now: datetime, reason: str = "",
             stale_after: float = DEFAULT_STALE_SECONDS) -> dict[str, Any]:
    status = "HISTORICAL"
    if as_of is not None:
        age = (now - as_of).total_seconds()
        if age < 0:
            raise JournalMismatch(f"{source} is stamped in the future")
        status = "STALE" if age > stale_after else "OBSERVED"
    out: dict[str, Any] = {
        "value": value, "unit": unit, "source": source, "scope": scope,
        "as_of": as_of.isoformat() if as_of else None, "status": status,
        "reason": reason, "kind": "number",
    }
    if as_of is not None:
        out["stale_after_seconds"] = stale_after
    return out


def _fill_count(state_dir: Path, session_id: str) -> tuple[int, datetime | None]:
    path = state_dir / "paper_fills.jsonl"
    if not path.exists():
        return 0, None
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue                      # a torn line is skipped, never counted as a fill
        if row.get("session_id") == session_id and row.get("fill_time"):
            rows.append(row)
    if not rows:
        return 0, None
    return len(rows), max(_parse(r["fill_time"]) for r in rows)


def _approval_counts(db: Path, now: datetime) -> dict[str, Any]:
    """Counts over the whole approval database — which spans sessions, and says so in its scope."""
    if not db.exists():
        return {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT issued_at, expires_at, resolved_at, verdict "
                            "FROM intents").fetchall()
    finally:
        conn.close()
    scope = (f"Entire approval database {db.name}; spans every session that used it, "
             "not only the one running now")
    pending = sum(1 for r in rows if r[3] is None and r[1] and _parse(r[1]) > now)
    stamp = now
    return {
        INTENTS: _reading(len(rows), "intents", f"{db.name}:intents", stamp, scope, now=now),
        ACCEPTED: _reading(sum(1 for r in rows if r[3] == "ACCEPT"), "decisions",
                           f"{db.name}:intents.verdict", stamp, scope, now=now),
        DECLINED: _reading(sum(1 for r in rows if r[3] == "DECLINE"), "decisions",
                           f"{db.name}:intents.verdict", stamp, scope, now=now),
        PENDING: _reading(pending, "prompts", f"{db.name}:unexpired unresolved", stamp, scope,
                          now=now),
        PROMPTS_WAITING: _reading(pending, "prompts", f"{db.name}:unexpired unresolved", stamp,
                                  scope, now=now),
        PASSBOOK: _reading(sum(1 for r in rows if r[3] is not None), "records",
                           f"{db.name}:resolved verdicts", stamp, scope, now=now,
                           reason="Full retained database count, not a page of the panel."),
    }


def snapshot(state_dir: Path, session_id: str, currency: str, *,
             approval_db: Path | None = None, now: datetime | None = None,
             stale_after: float = DEFAULT_STALE_SECONDS) -> dict[str, Any]:
    """A meter snapshot in the atlas's wire schema, read from the journals.

    The same shape ``export_meters.py`` writes to a file, so the ATLAS tab treats a live snapshot
    and an uploaded one identically — and holds both to the same rules: every reading carries the
    file it came from, the scope it covers and the moment it was observed.
    """
    now = now or datetime.now(UTC)
    readings: dict[str, Any] = {}
    notes: list[str] = []

    equity = read_equity(state_dir, session_id)
    if equity is None:
        notes.append("No equity rows for this session yet; portfolio meters are unread, not zero.")
    else:
        src = "paper_equity.csv"
        scope = f"Paper session {session_id}; currency {currency} supplied by the operator"
        at = equity.at
        readings[NAV] = _reading(equity.equity, currency, f"{src}:equity", at, scope, now=now,
                                 stale_after=stale_after)
        readings[CASH] = _reading(equity.cash, currency, f"{src}:cash", at, scope, now=now,
                                  stale_after=stale_after)
        readings[PNL] = _reading(
            equity.realized_pnl + equity.unrealized_pnl, currency,
            f"{src}:realized_pnl + unrealized_pnl", at, scope, now=now, stale_after=stale_after,
            reason="Journal P&L components, not total return; fees may sit outside them.")
        if equity.equity > 0:
            readings[EXPOSURE] = _reading(
                100.0 * equity.positions_value / equity.equity, "%",
                f"{src}:positions_value/equity", at, scope, now=now, stale_after=stale_after,
                reason="Signed net exposure; gross cannot be inferred from it.")
        readings[DRAWDOWN] = _reading(
            equity.max_drawdown_pct, "%", f"{src}:drawdown_pct", at, scope, now=now,
            stale_after=stale_after, reason="Drawdown at this mark, not the session maximum.")
        if equity.peak_equity > 0:
            readings[PEAK_RATIO] = _reading(
                100.0 * equity.equity / equity.peak_equity, "%", f"{src}:equity/peak_equity",
                at, scope, now=now, stale_after=stale_after)

    fills, last_fill = _fill_count(state_dir, session_id)
    if last_fill is not None:
        readings[FILLS] = _reading(
            fills, "fills", "paper_fills.jsonl", last_fill, f"Paper session {session_id}",
            now=now, stale_after=stale_after,
            reason="Journal records through the last fill; not proof of current liveness.")

    db = approval_db if approval_db is not None else state_dir / "approval.db"
    readings.update(_approval_counts(db, now))

    notes.append("Opening equity and external flows are not journalled, so return is unavailable "
                 "by design rather than by omission.")
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "PAPER SESSION",
        "session_id": session_id,
        "captured_at": now.isoformat(),
        "readings": readings,
        "notes": notes,
    }
