"""Avellaneda-Stoikov optimal market making (2008).

A market maker quoting a two-sided market faces one tension: tight quotes fill often but pile up
inventory that the price can move against; wide quotes are safe but rarely trade. A-S solves the
stochastic-control problem and gives closed-form quotes around a risk-adjusted *reservation price*
that skews away from the maker's current inventory:

    reservation   r = s - q * gamma * sigma^2 * (T - t)
    optimal spread    delta_a + delta_b = gamma * sigma^2 * (T - t) + (2/gamma) * ln(1 + gamma/k)

``gamma`` is risk aversion, ``q`` signed inventory, ``k`` the order-book fill-decay (from the fill
model), ``(T-t)`` time to the horizon. As inventory grows long the reservation price drops, skewing
quotes down so the ask is likelier to hit and reduce inventory — the model manages inventory risk
automatically, which a fixed symmetric spread cannot. :func:`simulate_market_making` runs either
policy through :mod:`.simulator` so the two can be compared on identical price paths.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .simulator import FillModel, MidPriceProcess


@dataclass(frozen=True)
class ASParams:
    gamma: float = 0.1     # inventory risk aversion
    sigma: float = 2.0     # mid-price volatility (match the price process)
    k: float = 1.5         # order-book liquidity decay (match the fill model)
    horizon: float = 1.0   # T, the trading horizon


@dataclass(frozen=True)
class MarketMakingResult:
    final_pnl: float
    pnl: np.ndarray          # mark-to-market P&L path
    inventory: np.ndarray    # signed inventory path
    mid: np.ndarray          # realized mid-price path
    n_bid_fills: int
    n_ask_fills: int

    @property
    def inventory_std(self) -> float:
        return float(np.std(self.inventory))

    @property
    def max_abs_inventory(self) -> float:
        return float(np.max(np.abs(self.inventory)))


def avellaneda_stoikov_quotes(s: float, q: float, t_remaining: float, p: ASParams) -> tuple[float, float, float, float]:
    """Return ``(bid, ask, reservation_price, total_spread)`` for mid ``s``, inventory ``q``,
    time-to-horizon ``t_remaining``."""
    reservation = s - q * p.gamma * p.sigma ** 2 * t_remaining
    spread = p.gamma * p.sigma ** 2 * t_remaining + (2.0 / p.gamma) * np.log1p(p.gamma / p.k)
    return reservation - spread / 2.0, reservation + spread / 2.0, reservation, spread


def simulate_market_making(params: ASParams, mid: MidPriceProcess, fill: FillModel, *, steps: int,
                           rng: np.random.Generator, symmetric_half_spread: float | None = None,
                           inventory_limit: int | None = None) -> MarketMakingResult:
    """Run a market-making policy over one simulated price path.

    With ``symmetric_half_spread=None`` (default) it quotes the Avellaneda-Stoikov policy; pass a
    number to instead quote a fixed symmetric spread around the mid (the naive baseline). Fills are
    drawn from ``fill`` at each side's distance from the mid. ``inventory_limit`` optionally stops
    quoting the side that would push inventory past +/- the limit.
    """
    path = mid.path(steps, rng)
    q = 0
    cash = 0.0
    inv = np.zeros(steps + 1)
    pnl = np.zeros(steps + 1)
    n_bid = n_ask = 0
    for i in range(steps):
        s = float(path[i])
        if symmetric_half_spread is None:
            t_remaining = params.horizon * (1.0 - i / steps)
            bid, ask, _, _ = avellaneda_stoikov_quotes(s, q, t_remaining, params)
        else:
            bid, ask = s - symmetric_half_spread, s + symmetric_half_spread
        can_buy = inventory_limit is None or q < inventory_limit
        can_sell = inventory_limit is None or q > -inventory_limit
        if can_buy and fill.fills(s - bid, mid.dt, rng):
            q += 1
            cash -= bid
            n_bid += 1
        if can_sell and fill.fills(ask - s, mid.dt, rng):
            q -= 1
            cash += ask
            n_ask += 1
        inv[i + 1] = q
        pnl[i + 1] = cash + q * float(path[i + 1])
    return MarketMakingResult(final_pnl=cash + q * float(path[-1]), pnl=pnl, inventory=inv,
                              mid=path, n_bid_fills=n_bid, n_ask_fills=n_ask)
