"""Dynamic dollar-hedge overlay — scale a USD (UUP) sleeve up as the book draws down.

The FX hedge study found the US dollar (UUP) is the one reliable risk-off diversifier for
the equity book (correlation ~ -0.24 over 5y): a static 20-30% sleeve cut max drawdown by
~11 points but permanently capped upside. This overlay makes the hedge *dynamic* — near
zero when the book is at its highs (keep full equity upside), ramping toward a cap as the
drawdown deepens (buy protection exactly when a risk-off move is underway), with an
optional boost from portfolio heat.

Everything here is a pure function of portfolio state (drawdown, heat), so it runs and
tests with no broker. ``hedge_weight`` yields the target sleeve fraction; ``hedge_shares``
turns that into a share count; ``rebalance_delta`` adds a no-trade band so the sleeve
isn't churned on every wiggle.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class HedgePolicy:
    """Drawdown-driven dollar-hedge schedule.

    The hedge weight is ``base_weight`` while the book's drawdown is shallower than
    ``ramp_start``, rises linearly to ``max_weight`` as drawdown deepens to ``ramp_full``,
    and is capped there. ``heat_boost`` optionally adds weight per unit of portfolio heat
    above ``heat_ref``. Drawdowns are signed (<= 0), e.g. ``-0.10`` for a 10% drawdown.
    """

    symbol: str = "UUP"
    base_weight: float = 0.0
    max_weight: float = 0.30
    ramp_start: float = -0.05
    ramp_full: float = -0.20
    heat_boost: float = 0.0
    heat_ref: float = 0.05

    def __post_init__(self) -> None:
        if not 0.0 <= self.base_weight <= self.max_weight <= 1.0:
            raise ValueError("require 0 <= base_weight <= max_weight <= 1")
        if not (self.ramp_start <= 0.0 and self.ramp_full < self.ramp_start):
            raise ValueError("require ramp_full < ramp_start <= 0 (both drawdowns)")


def hedge_weight(drawdown: float, *, policy: HedgePolicy | None = None, heat: float = 0.0) -> float:
    """Target dollar-hedge fraction in ``[0, max_weight]`` from drawdown (<= 0) and heat."""
    p = policy or HedgePolicy()
    dd = min(float(drawdown), 0.0)
    if dd >= p.ramp_start:
        w = p.base_weight
    elif dd <= p.ramp_full:
        w = p.max_weight
    else:
        t = (p.ramp_start - dd) / (p.ramp_start - p.ramp_full)  # 0 at ramp_start -> 1 at ramp_full
        w = p.base_weight + t * (p.max_weight - p.base_weight)
    if p.heat_boost > 0.0:
        w += p.heat_boost * max(0.0, float(heat) - p.heat_ref)
    return float(min(max(w, 0.0), p.max_weight))


def hedge_shares(*, equity: float, hedge_price: float, target_weight: float) -> int:
    """Shares of the hedge ETF to hold so the sleeve is ``target_weight`` of equity."""
    if equity <= 0 or hedge_price <= 0 or target_weight <= 0:
        return 0
    return max(math.floor(equity * target_weight / hedge_price), 0)


def rebalance_delta(current_shares: int, target_shares: int, *, band: float = 0.20) -> int:
    """Shares to trade to reach ``target_shares``, or 0 inside a no-trade ``band``.

    The band is a fraction of the target position; small drifts don't trigger a trade, so
    the sleeve isn't churned (and its costs kept down) on every small move in drawdown.
    """
    if target_shares <= 0:
        return -current_shares  # close the sleeve
    tol = max(round(band * target_shares), 1)
    if abs(target_shares - current_shares) < tol:
        return 0
    return target_shares - current_shares
