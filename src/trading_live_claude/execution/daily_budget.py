"""Per-day trade-count and notional caps for autonomous mode.

Reads state/orders.jsonl + state/fills.jsonl to count today's activity.
Idempotent: safe to read on every order intent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path


@dataclass(frozen=True)
class DailyBudgetState:
    trades_today: int
    notional_today_usd: float
    max_trades: int
    max_notional_usd: float

    @property
    def trades_remaining(self) -> int:
        return max(self.max_trades - self.trades_today, 0)

    @property
    def notional_remaining(self) -> float:
        return max(self.max_notional_usd - self.notional_today_usd, 0.0)

    def admits(self, *, additional_notional_usd: float) -> tuple[bool, str]:
        if self.trades_today >= self.max_trades:
            return False, f"daily trade cap reached ({self.trades_today}/{self.max_trades})"
        if self.notional_today_usd + additional_notional_usd > self.max_notional_usd:
            return False, (
                f"daily notional cap would be exceeded "
                f"(${self.notional_today_usd:,.0f} + ${additional_notional_usd:,.0f} > ${self.max_notional_usd:,.0f})"
            )
        return True, ""


class DailyBudget:
    def __init__(
        self,
        state_dir: Path,
        max_trades_per_day: int = 10,
        max_notional_per_day_usd: float = 10_000.0,
    ) -> None:
        self.state_dir = state_dir
        self.max_trades = max_trades_per_day
        self.max_notional = max_notional_per_day_usd

    def _today(self) -> date:
        return datetime.now(UTC).date()

    def snapshot(self) -> DailyBudgetState:
        trades = 0
        notional = 0.0
        today = self._today()
        for filename in ("orders.jsonl",):
            path = self.state_dir / filename
            if not path.exists():
                continue
            try:
                with path.open(encoding="utf-8") as f:
                    for line in f:
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not row.get("accepted", False):
                            continue
                        ts_str = row.get("ts") or ""
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        if ts.date() != today:
                            continue
                        trades += 1
                        notional += float(row.get("shares", 0)) * float(row.get("entry", 0.0))
            except OSError:
                continue
        return DailyBudgetState(
            trades_today=trades,
            notional_today_usd=notional,
            max_trades=self.max_trades,
            max_notional_usd=self.max_notional,
        )
