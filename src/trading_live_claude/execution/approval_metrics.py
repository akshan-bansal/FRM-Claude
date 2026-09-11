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
        ttl_deltas = []
        for entry in passbook:
            # Estimate from issued_at in pending store
            # (passbook doesn't track issued_at, would need schema change)
            pass
        avg_ttl = 4.2  # TODO: compute from prompt timestamps

        # Equity metrics (from broker or journal)
        starting_equity = 100_000.0  # TODO: pull from router.broker.equity()
        session_equity = 100_847.0  # TODO: compute from fills
        peak_equity = 100_847.0  # TODO: track running max
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
        """
        # TODO: integrate with allocator state to get live conviction scores
        # For now, return demo matrix
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

        # Demo matrix (would be live from allocator + conviction engine)
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

    def _count_gate_rejections(self) -> int:
        """Count rejected orders from the journal."""
        if not self.journal.rejected_path.exists():
            return 0

        count = 0
        try:
            with self.journal.rejected_path.open("r") as f:
                for line in f:
                    if line.strip():
                        count += 1
        except (OSError, IOError):
            pass

        return count

    def _last_gate_rejection_reason(self) -> str:
        """Get the most recent gate rejection reason."""
        if not self.journal.rejected_path.exists():
            return ""

        try:
            with self.journal.rejected_path.open("r") as f:
                lines = f.readlines()
                if lines:
                    last = json.loads(lines[-1].strip())
                    return last.get("reason", "")
        except (OSError, IOError, json.JSONDecodeError):
            pass

        return ""

    def compute_session_equity(self) -> tuple[float, float, float]:
        """Compute current, peak, and starting equity from journal fills.

        Returns (starting_equity, session_equity, peak_equity).
        """
        # TODO: parse fills.jsonl and compute equity progression
        # For now, return placeholder values
        return 100_000.0, 100_847.0, 100_847.0

    def get_avg_ttl_response(self) -> float:
        """Compute median card response time from passbook.

        Returns average TTL in seconds.
        """
        # TODO: extract TTL deltas from passbook resolved_at vs. prompt issued_at
        # Would need to store issued_at in passbook or query pending store
        return 4.2
