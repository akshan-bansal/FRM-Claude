"""Cross-market (statistical) arbitrage between two venues quoting the same asset.

Two venues share a common efficient price but each carries idiosyncratic noise, so their mids
diverge and reconverge. When the gap exceeds the round-trip cost by a threshold, the arbitrageur
buys the cheaper venue and sells the dearer one, then unwinds when the gap closes — capturing the
divergence minus costs. This is the simulated, single-asset version of cross-market arb; a real
implementation needs synchronized L2 feeds and low-latency execution on both venues, which the
current stack does not have.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class ArbConfig:
    s0: float = 100.0
    sigma: float = 1.0          # common-factor volatility (per sqrt-step)
    noise: float = 0.05         # per-venue idiosyncratic noise sd
    dt: float = 1.0 / 390.0
    entry_gap: float = 0.10     # open when |mid_a - mid_b| exceeds this
    exit_gap: float = 0.02      # unwind when the gap closes to this
    cost: float = 0.01          # round-trip cost charged per leg on entry


@dataclass(frozen=True)
class ArbResult:
    pnl: float
    n_trades: int
    mid_a: np.ndarray
    mid_b: np.ndarray
    gap: np.ndarray
    trades: list[tuple[int, int, float]] = field(default_factory=list)  # (entry_i, exit_i, captured)

    @property
    def hit_rate(self) -> float:
        if not self.trades:
            return 0.0
        return float(np.mean([1.0 if t[2] > 0 else 0.0 for t in self.trades]))


def cross_market_arbitrage(cfg: ArbConfig, *, steps: int, rng: np.random.Generator) -> ArbResult:
    """Simulate ``steps`` of two correlated venue mids and trade the divergence.

    Returns realized P&L net of costs, the per-trade log, and the two mid paths for plotting.
    """
    common = cfg.s0 + np.concatenate([[0.0], np.cumsum(cfg.sigma * np.sqrt(cfg.dt) * rng.standard_normal(steps))])
    mid_a = common + rng.normal(0.0, cfg.noise, steps + 1)
    mid_b = common + rng.normal(0.0, cfg.noise, steps + 1)
    gap = mid_a - mid_b

    pnl = 0.0
    position = 0            # +1 = long A / short B, -1 = short A / long B
    entry_i = 0
    entry_gap_val = 0.0
    trades: list[tuple[int, int, float]] = []
    for i in range(steps + 1):
        g = float(gap[i])
        if position == 0:
            if abs(g) > cfg.entry_gap:
                position = -1 if g > 0 else 1   # fade the gap: short the dearer venue, long the cheaper
                entry_i, entry_gap_val = i, g
                pnl -= 2.0 * cfg.cost            # cost on both legs
        elif abs(g) <= cfg.exit_gap:
            captured = abs(entry_gap_val) - abs(g)   # convergence captured
            pnl += captured
            trades.append((entry_i, i, captured - 2.0 * cfg.cost))
            position = 0
    return ArbResult(pnl=float(pnl), n_trades=len(trades), mid_a=mid_a, mid_b=mid_b, gap=gap, trades=trades)
