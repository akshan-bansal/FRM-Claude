"""ATR-based fixed-fractional position sizing (article skill #4 recipe step 1-2)."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SizingResult:
    shares: int
    entry: float
    stop: float
    target: float | None
    dollar_risk: float
    r_multiple_target: float | None


class PositionSizer:
    """Compute share quantity given equity, ATR, and risk % per trade.

    Formula (long example):
        stop_distance = atr_multiple * atr
        dollar_risk   = equity * risk_pct
        shares        = floor(dollar_risk / stop_distance)
    """

    def __init__(self, risk_pct: float = 0.01, atr_multiple: float = 2.0, target_r: float = 2.0) -> None:
        if not 0.0 < risk_pct <= 0.1:
            raise ValueError(f"risk_pct must be in (0, 0.1]; got {risk_pct}")
        self.risk_pct = risk_pct
        self.atr_multiple = atr_multiple
        self.target_r = target_r

    def size(
        self,
        *,
        equity: float,
        entry: float,
        atr_value: float,
        side: str = "long",
    ) -> SizingResult:
        if equity <= 0 or entry <= 0 or atr_value <= 0:
            raise ValueError("equity, entry, atr_value must be positive")

        stop_distance = self.atr_multiple * atr_value
        if side == "long":
            stop = entry - stop_distance
            target = entry + self.target_r * stop_distance
        else:
            stop = entry + stop_distance
            target = entry - self.target_r * stop_distance

        dollar_risk = equity * self.risk_pct
        shares = max(math.floor(dollar_risk / stop_distance), 0)
        return SizingResult(
            shares=shares,
            entry=entry,
            stop=stop,
            target=target,
            dollar_risk=shares * stop_distance,
            r_multiple_target=self.target_r,
        )
