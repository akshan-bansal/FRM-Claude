"""Portfolio heat: aggregate open-risk exposure across all positions."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HeatSnapshot:
    total_exposure: float
    equity: float
    heat_pct: float
    breached: bool


class PortfolioHeat:
    def __init__(self, cap_pct: float = 0.05) -> None:
        if not 0.0 < cap_pct <= 1.0:
            raise ValueError(f"cap_pct must be in (0, 1]; got {cap_pct}")
        self.cap_pct = cap_pct

    def snapshot(self, *, equity: float, open_risk_dollars: float) -> HeatSnapshot:
        if equity <= 0:
            return HeatSnapshot(total_exposure=open_risk_dollars, equity=equity, heat_pct=float("inf"), breached=True)
        heat_pct = open_risk_dollars / equity
        return HeatSnapshot(
            total_exposure=open_risk_dollars,
            equity=equity,
            heat_pct=heat_pct,
            breached=heat_pct > self.cap_pct,
        )

    def admits(self, *, equity: float, existing_risk: float, additional_risk: float) -> bool:
        return self.snapshot(equity=equity, open_risk_dollars=existing_risk + additional_risk).breached is False
