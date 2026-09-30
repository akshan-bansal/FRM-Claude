"""Real-time metrics extraction for the BI dashboard.

Queries the approval store, router journal, and allocator state to
populate /v1/stats and /v1/conviction-matrix endpoints.
"""
from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..audit.meters import JournalMismatch, read_equity

if TYPE_CHECKING:
    from .journal import OrderJournal
    from .router import Router

from ..logging_setup import get_logger

log = get_logger(__name__)


class ApprovalMetrics:
    """Extract operational metrics from approval store and journal."""

    def __init__(
        self,
        store,  # InMemoryApprovalStore | SqliteApprovalStore
        journal: OrderJournal,
        router: Router | None = None,
        *,
        state_dir: Path | None = None,
        session_id: str | None = None,
        account_currency: str = "USD",
    ) -> None:
        self.store = store
        self.journal = journal
        self.router = router
        # Which book to report on, and where its journals are. Without them the equity fields are
        # null: a shim that does not know its session has nothing to say about that session's
        # equity, and saying 100,000 instead is what this replaced.
        self.state_dir = state_dir
        self.session_id = session_id
        self.account_currency = account_currency

    def get_stats(self) -> dict[str, Any]:
        """Compute real-time metrics for /v1/stats endpoint.

        Returns equity, approval rate, TTL response, gate rejections, overlay.
        """
        passbook = self.store.passbook(limit=10000, offset=0)
        pending = self.store.pending()

        # Count verdicts
        accepted = sum(1 for e in passbook if e.verdict == "ACCEPT")
        declined = sum(1 for e in passbook if e.verdict == "DECLINE")
        expired = sum(1 for e in passbook if e.verdict == "EXPIRED")
        total = len(passbook)

        # Compute acceptance rate (non-expired)
        decided = accepted + declined
        acceptance_rate = accepted / (decided or 1) if decided > 0 else 0.0

        # Avg TTL response (from passbook entry timestamps)
        avg_ttl = self.get_avg_ttl_response()

        # Equity: read from the session's own journal rows, or reported as unread.
        snap = self.equity_snapshot()

        # Gate rejections (from journal)
        gate_rejects = self._count_gate_rejections()
        last_gate_reason = self._last_gate_rejection_reason()

        # Overlay scalar (from router or store context)
        # Fields with no live source are reported as null and named in `placeholders`, not filled
        # with a plausible-looking number. A dashboard that read 0.47 as the live overlay scalar —
        # the previous behaviour — would be showing invented risk state as measured risk state.
        placeholders = ["overlay_scalar", "overlay_risk_zone"]
        if self.session_id is None:
            placeholders.append("session_id")
        if snap is None:
            # No marked row for this session: the book has no equity to report yet.
            placeholders += ["starting_equity", "session_equity", "peak_equity",
                             "max_drawdown_pct"]
        if avg_ttl is None:
            # No prompt has been decided yet, so there is no median to report.
            placeholders.append("avg_ttl_response")

        return {
            "session_id": self.session_id,
            "starting_equity": snap.peak_equity if snap else None,
            "session_equity": snap.equity if snap else None,
            "peak_equity": snap.peak_equity if snap else None,
            "max_drawdown_pct": snap.max_drawdown_pct if snap else None,
            "acceptance_rate": float(acceptance_rate),
            "intents_total": total,
            "intents_approved": accepted,
            "intents_declined": declined,
            "intents_expired": expired,
            "intents_pending": len(pending),
            "avg_ttl_response": avg_ttl,
            "gate_rejections": gate_rejects,
            "last_gate_reason": last_gate_reason,
            "overlay_scalar": None,
            "overlay_risk_zone": None,
            "placeholders": placeholders,
        }

    def get_conviction_matrix(self) -> dict[str, Any]:
        """Walk-forward scores per (symbol, strategy) for /v1/conviction-matrix.

        Rebuilt 2026-09-25 to stop inventing. What it used to do:

        * hard-coded 13 symbols including SPY and BTC/USD, **neither of which has a walk-forward
          score at all**, and gave every unvalidated symbol a flat 0.5 "neutral conviction";
        * derived five per-strategy columns from one number with fixed offsets — `+0.15` because
          "momentum tends higher", `-0.20` for a "bearish overlay" — presenting invented
          differentiation as measured conviction;
        * clamped the result into [0, 1], which hid that `oos_score` is an unbounded ratio around
          19-28 for validated names, so every real symbol saturated at 1.0;
        * and fell back to a hard-coded demo grid when none of that was available.

        Now it reports only what `WALK_FORWARD_VALIDATED` measured: the score for the
        (symbol, strategy) pair that was actually validated, and **null everywhere else**. The holes
        are the point — a sparse grid says which pairs have evidence, where a full grid of plausible
        numbers said nothing true. Per-strategy conviction is not computed anywhere in this codebase,
        so this endpoint does not pretend otherwise.
        """
        try:
            from ..analysis.universe import WALK_FORWARD_VALIDATED as validated
        except ImportError:                      # pragma: no cover - registry always ships
            return {"symbols": [], "strategies": [], "matrix": [], "available": False,
                    "source": "analysis.universe.WALK_FORWARD_VALIDATED",
                    "metric": "", "note": "walk-forward registry unavailable",
                    "updated_at": datetime.now(UTC).isoformat()}

        entries = sorted(validated.values(), key=lambda v: -v.oos_score)
        symbols = [v.symbol for v in entries]
        strategies = sorted({v.strategy for v in entries})
        by_symbol = {v.symbol: v for v in entries}
        matrix: list[list[float | None]] = [
            [round(by_symbol[sym].oos_score, 3) if by_symbol[sym].strategy == strat else None
             for strat in strategies]
            for sym in symbols
        ]
        return {
            "symbols": symbols,
            "strategies": strategies,
            "matrix": matrix,
            "available": bool(symbols),
            "source": "analysis.universe.WALK_FORWARD_VALIDATED",
            "metric": "out-of-sample score (unbounded ratio, NOT normalised to 0-1)",
            "note": ("One value per validated (symbol, strategy) pair; null means that pair was "
                     "never walk-forward validated, not that its conviction is zero. Per-strategy "
                     "conviction is not computed in this codebase."),
            "tiers": {v.symbol: v.tier for v in entries},
            "updated_at": datetime.now(UTC).isoformat(),
        }

        try:
            # Attempt to load walk-forward validated scores from universe
            from ..analysis.universe import WALK_FORWARD_VALIDATED

            # Base conviction from oos_score (allocator perspective)
            scores = {}
            for sym in symbols:
                if sym in WALK_FORWARD_VALIDATED:
                    scores[sym] = max(WALK_FORWARD_VALIDATED[sym].oos_score, 0.0)
                else:
                    scores[sym] = 0.5  # Default neutral conviction

            # Build matrix: each row is a symbol, each column is a strategy
            # For now, all strategies get the same base score; future work will
            # wire per-strategy conviction from individual signal components
            matrix = []
            for sym in symbols:
                base = scores.get(sym, 0.5)
                # Mock strategy perspectives (normalized around base conviction)
                row = [
                    min(1.0, base + 0.15),  # momentum tends higher
                    max(0.0, base - 0.20),  # bearish overlay reduces
                    base,                    # mean-reversion neutral
                    base + 0.05,            # heat pulse slight boost
                    base,                    # allocator uses oos_score
                ]
                matrix.append(row)
            return matrix
        except (ImportError, AttributeError, KeyError):
            # Universe data not available; fall back to demo
            return None

    def _count_gate_rejections(self) -> int:
        """Count rejected orders from the journal."""
        if not self.journal.rejected_path.exists():
            return 0

        count = 0
        try:
            with self.journal.rejected_path.open("r") as f:
                for line in f:
                    if line.strip():
                        try:
                            json.loads(line)
                            count += 1
                        except json.JSONDecodeError:
                            pass
        except OSError:
            pass

        return count

    def _last_gate_rejection_reason(self) -> str:
        """Get the most recent gate rejection reason."""
        if not self.journal.rejected_path.exists():
            return ""

        try:
            with self.journal.rejected_path.open("r") as f:
                lines = [line.strip() for line in f if line.strip()]
                if lines:
                    try:
                        last = json.loads(lines[-1])
                        # Check for 'reason' field (gate rejection reason)
                        reason = last.get("reason", "")
                        if reason:
                            return reason
                        # Fallback to any string representation
                        return str(last.get("gate", "reason not recorded"))
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass

        return ""

    def equity_snapshot(self):
        """The session's latest marked row from ``state/paper_equity.csv``, or None.

        ``state/`` is ground truth for equity (CLAUDE.md), so this reads it. What it replaced
        walked the Router journal's fills from a hard-coded 100,000 and subtracted the full cost
        of every BUY without adding the position back, which meant a live panel showed the default
        and labelled it session equity.
        """
        if self.state_dir is None or not self.session_id:
            return None
        try:
            return read_equity(Path(self.state_dir), self.session_id)
        except (JournalMismatch, OSError, KeyError, ValueError):
            # A row that contradicts itself is reported as no reading, never as a rounded one.
            return None

    def compute_session_equity(self) -> tuple[float, float, float]:
        """Deprecated shape kept for callers that still want a 3-tuple. Zeros mean "unknown"."""
        snap = self.equity_snapshot()
        if snap is None:
            return 0.0, 0.0, 0.0
        return snap.peak_equity, snap.equity, snap.peak_equity

    def _legacy_fill_walk(self) -> tuple[float, float, float]:
        """The pre-2026-09-28 estimate. Unused; kept only to document what was wrong with it."""
        starting_equity = 100_000.0  # Default starting capital
        current_equity = starting_equity
        peak_equity = starting_equity

        # Parse fills.jsonl and compute P&L progression
        if self.journal.fills_path.exists():
            try:
                with self.journal.fills_path.open("r") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            fill = json.loads(line)
                            # Compute realized P&L from fill
                            qty = fill.get("qty", 0)
                            entry = fill.get("entry", 0)
                            fill_price = fill.get("price", entry)
                            action = fill.get("action", "BUY")

                            # P&L = qty * (price - entry) for SELL, negative for BUY
                            if action == "SELL":
                                pnl = qty * (fill_price - entry)
                            else:
                                pnl = -qty * fill_price  # Cost of BUY

                            current_equity += pnl
                            peak_equity = max(peak_equity, current_equity)
                        except (json.JSONDecodeError, KeyError, TypeError):
                            continue
            except OSError:
                pass

        return starting_equity, current_equity, peak_equity

    def get_avg_ttl_response(self) -> float | None:
        """Median seconds between a prompt being issued and the card answering it.

        Until 2026-09-28 this returned the constant 4.2 in every case: the loop that was meant to
        compute the deltas was a ``pass``, and the passbook did not carry ``issued_at`` anyway, so
        a dashboard reading "median card response 4.2s" was reading a literal. The passbook now
        carries the issue time, so this is measured.

        EXPIRED rows are excluded: their ``resolved_at`` is when the expiry sweep noticed them, not
        a decision by a holder. ``None`` when nothing has been decided — the median of no
        observations is not zero, and it is certainly not 4.2.
        """
        deltas = []
        for entry in self.store.passbook(limit=500, offset=0):
            if entry.verdict not in ("ACCEPT", "DECLINE") or entry.issued_at is None:
                continue
            took = (entry.resolved_at - entry.issued_at).total_seconds()
            if took >= 0:                       # a negative interval is a clock fault, not a speed
                deltas.append(took)
        return round(statistics.median(deltas), 2) if deltas else None
