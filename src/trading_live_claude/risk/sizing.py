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


@dataclass(frozen=True)
class VolTargetResult:
    shares: int
    notional: float
    vol_scale: float   # target_vol / annual_vol, after conviction and leverage cap
    conviction: float


def _clip01(x: float) -> float:
    return min(max(x, 0.0), 1.0)


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
        conviction: float = 1.0,
    ) -> SizingResult:
        """ATR fixed-fractional size. ``conviction`` in [0, 1] scales the risk budget, so a
        strategy's graded ``signal_strength`` sizes weak setups smaller than strong ones."""
        if equity <= 0 or entry <= 0 or atr_value <= 0:
            raise ValueError("equity, entry, atr_value must be positive")

        stop_distance = self.atr_multiple * atr_value
        if side == "long":
            stop = entry - stop_distance
            target = entry + self.target_r * stop_distance
        else:
            stop = entry + stop_distance
            target = entry - self.target_r * stop_distance

        dollar_risk = equity * self.risk_pct * _clip01(conviction)
        shares = max(math.floor(dollar_risk / stop_distance), 0)
        return SizingResult(
            shares=shares,
            entry=entry,
            stop=stop,
            target=target,
            dollar_risk=shares * stop_distance,
            r_multiple_target=self.target_r,
        )

    def size_vol_target(
        self,
        *,
        equity: float,
        price: float,
        annual_vol: float,
        target_vol: float = 0.15,
        conviction: float = 1.0,
        max_leverage: float = 1.0,
    ) -> VolTargetResult:
        """Volatility-targeted size: notional = equity x (target_vol / annual_vol), so every
        position contributes about the same risk regardless of how volatile the name is.

        A low-vol name gets a larger position, a high-vol name a smaller one; the scale is
        capped at ``max_leverage`` and multiplied by ``conviction`` in [0, 1]. ``annual_vol``
        is the name's annualized return volatility (e.g. daily std x sqrt(252))."""
        if equity <= 0 or price <= 0 or annual_vol <= 0:
            raise ValueError("equity, price, annual_vol must be positive")
        vol_scale = min((target_vol / annual_vol) * _clip01(conviction), max_leverage)
        notional = equity * vol_scale
        shares = max(math.floor(notional / price), 0)
        return VolTargetResult(shares=shares, notional=shares * price, vol_scale=vol_scale, conviction=_clip01(conviction))
