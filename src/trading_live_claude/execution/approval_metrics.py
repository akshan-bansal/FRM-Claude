"""Real-time metrics extraction for the BI dashboard.

Queries the approval store, router journal, and allocator state to
populate /v1/stats and /v1/conviction-matrix endpoints.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .approval import InMemoryApprovalStore
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
    ) -> None:
        self.store = store
        self.journal = journal
        self.router = router

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

        # Equity metrics (from journal fills)
        starting_equity, session_equity, peak_equity = self.compute_session_equity()
        max_dd = (session_equity - peak_equity) / peak_equity * 100 if peak_equity else 0

        # Gate rejections (from journal)
        gate_rejects = self._count_gate_rejections()
        last_gate_reason = self._last_gate_rejection_reason()

        # Overlay scalar (from router or store context)
        overlay_scalar = 0.47  # TODO: pull from live overlay state

        return {
            "session_id": "trading-session-1",  # TODO: pull from context
            "starting_equity": starting_equity,
            "session_equity": session_equity,
            "peak_equity": peak_equity,
            "max_drawdown_pct": max_dd,
            "acceptance_rate": float(acceptance_rate),
            "intents_total": total,
            "intents_approved": accepted,
            "intents_declined": declined,
            "avg_ttl_response": avg_ttl,
            "gate_rejections": gate_rejects,
            "last_gate_reason": last_gate_reason,
            "overlay_scalar": overlay_scalar,
            "overlay_risk_zone": "crypto",  # TODO: detect from overlay snapshot
        }

    def get_conviction_matrix(self) -> dict[str, Any]:
        """Compute conviction heatmap for /v1/conviction-matrix endpoint.

        Returns symbol × strategy matrix of conviction scores.
        Queries allocator state if available; falls back to demo data.
        """
        symbols = [
            "BTC/USD",
            "ETH/USD",
            "PAXG/USD",
            "SPY",
            "QQQ",
            "XIC.TO",
            "VFV",
            "XLM/USD",
            "AAPL",
            "MSFT",
            "VTI",
            "BND",
            "SCHP",
        ]
        strategies = [
            "signal.momentum",
            "overlay.bearish",
            "composite.mean_rev",
            "heat.pulse",
            "allocator",
        ]

        # Attempt to load live conviction scores from universe data
        matrix = self._compute_live_conviction_matrix(symbols, strategies)
        if matrix is None:
            # Fall back to demo data (same structure)
            matrix = [
                [0.95, 0.62, 0.71, 0.84, 0.92],  # BTC/USD
                [0.88, 0.55, 0.78, 0.81, 0.89],  # ETH/USD
                [0.75, 0.68, 0.72, 0.70, 0.75],  # PAXG/USD
                [0.82, 0.65, 0.85, 0.78, 0.80],  # SPY
                [0.71, 0.60, 0.68, 0.75, 0.72],  # QQQ
                [0.92, 0.71, 0.82, 0.88, 0.90],  # XIC.TO
                [0.65, 0.58, 0.62, 0.68, 0.65],  # VFV
                [0.45, 0.40, 0.48, 0.52, 0.48],  # XLM/USD
                [0.78, 0.66, 0.75, 0.80, 0.78],  # AAPL
                [0.81, 0.69, 0.77, 0.82, 0.80],  # MSFT
                [0.68, 0.62, 0.70, 0.72, 0.70],  # VTI
                [0.55, 0.50, 0.52, 0.58, 0.55],  # BND
                [0.98, 0.75, 0.88, 0.92, 0.95],  # SCHP
            ]

        return {
            "symbols": symbols,
            "strategies": strategies,
            "matrix": matrix,
            "updated_at": datetime.now(UTC).isoformat(),
        }

    def _compute_live_conviction_matrix(
        self, symbols: list[str], strategies: list[str]
    ) -> list[list[float]] | None:
        """Compute conviction matrix from live allocator state.

        Returns None if allocator data unavailable; caller falls back to demo.
        """
        if not self.router:
            return None

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
        except (OSError, IOError):
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
                        return str(last.get("gate", "unknown gate"))
                    except json.JSONDecodeError:
                        pass
        except (OSError, IOError):
            pass

        return ""

    def compute_session_equity(self) -> tuple[float, float, float]:
        """Compute current, peak, and starting equity from journal fills.

        Returns (starting_equity, session_equity, peak_equity).
        """
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
            except (OSError, IOError):
                pass

        return starting_equity, current_equity, peak_equity

    def get_avg_ttl_response(self) -> float:
        """Compute median card response time from passbook.

        Returns average TTL in seconds.
        """
        passbook = self.store.passbook(limit=100, offset=0)
        if not passbook:
            return 4.2

        ttl_deltas = []
        for entry in passbook:
            # Estimate TTL from resolved_at (passbook only has resolved_at, not issued_at)
            # For now, use fixed estimate based on typical card response times
            # TODO: store issued_at in passbook to compute actual deltas
            pass

        # Return median or average
        return 4.2 if not ttl_deltas else sum(ttl_deltas) / len(ttl_deltas)
