"""Pairs / cointegration trading.

Two strategies here, both single-leg representations: the engine reads ``entry``/``exit`` on
the spread and treats an entry as "long leg A vs short leg B" (the ``size_hint``/``close_b``
columns carry the partner). Live execution of a real pair needs a paired router, still out of
scope, so these stay research/backtest strategies.

* :class:`PairsZScore` — the original static hedge (fixed 1:1 log-spread, window z-score).
* :class:`KalmanPairs` — the fixed hedge is the thing that breaks in practice: the ratio drifts
  and the spread stops reverting. This filters a *time-varying* hedge ratio with a Kalman filter
  (:mod:`...models.kalman`) and trades the self-normalizing innovation z-score, optionally gated
  on an Engle-Granger cointegration test so it only fires on legs that actually revert.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ...models.cointegration import engle_granger
from ...models.kalman import KalmanHedge
from ...signals.indicators import zscore
from ..base import Strategy, StrategyContext


class PairsZScore(Strategy):
    name = "pairs"
    description = "Z-score spread reversion, static hedge (research-only; needs paired router)"

    def __init__(self, window: int = 30, entry_z: float = 2.0, exit_z: float = 0.5) -> None:
        super().__init__(window=window, entry_z=entry_z, exit_z=exit_z)
        self.window = window
        self.entry_z = entry_z
        self.exit_z = exit_z

    def required_history_bars(self) -> int:
        return max(self.window * 6, 120)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        if "close_b" not in df.columns:
            raise ValueError("Pairs strategy needs a 'close_b' column from the partner leg.")
        out = df.copy()
        out["spread"] = np.log(out["close"]) - np.log(out["close_b"])
        out["spread_z"] = zscore(out["spread"], self.window)
        prev = out["spread_z"].shift(1)
        out["entry"] = ((prev <= -self.entry_z) & (out["spread_z"] > -self.entry_z)).astype(int)
        out["exit"] = (out["spread_z"].abs() <= self.exit_z).astype(int)
        out["size_hint"] = 1.0
        out["atr"] = (out["spread"].rolling(self.window).std() * out["close"]).fillna(out["close"] * 0.02)
        return out


class KalmanPairs(Strategy):
    """Kalman-filtered time-varying hedge ratio; trades the standardized innovation.

    Entry fires when the spread z-score reverts up through ``-entry_z`` from an oversold spread
    (long A / short B); exit when the spread is back near fair value (``|z| <= exit_z``). When
    ``require_cointegration`` is set, entries are suppressed unless the full-sample Engle-Granger
    test passes — a conservative gate that keeps the strategy off non-reverting pairs.
    """

    name = "kalman_pairs"
    description = "Kalman time-varying hedge ratio + Engle-Granger gate (research-only)"

    def __init__(self, delta: float = 1e-4, entry_z: float = 2.0, exit_z: float = 0.5,
                 warmup: int = 30, z_window: int = 30, require_cointegration: bool = True,
                 coint_alpha: float = 0.10) -> None:
        super().__init__(delta=delta, entry_z=entry_z, exit_z=exit_z, warmup=warmup,
                         z_window=z_window, require_cointegration=require_cointegration,
                         coint_alpha=coint_alpha)
        self.delta = delta
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.warmup = warmup
        self.z_window = z_window
        self.require_cointegration = require_cointegration
        self.coint_alpha = coint_alpha

    def required_history_bars(self) -> int:
        return max(self.warmup * 4, self.z_window * 4, 120)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        if "close_b" not in df.columns:
            raise ValueError("KalmanPairs needs a 'close_b' column from the partner leg.")
        out = df.copy()
        y = np.log(out["close"].to_numpy(dtype=float))
        x = np.log(out["close_b"].to_numpy(dtype=float))
        state = KalmanHedge(delta=self.delta, warmup=self.warmup).filter(y, x)

        out["hedge_ratio"] = state.beta
        out["spread"] = state.spread
        # Trade a rolling z-score of the Kalman spread (innovation), not the filter's own
        # innovation variance: the latter is inflated by the price-level term and never reaches
        # the entry band. The spread itself is causal (one-step prediction error), so the z is too.
        out["spread_z"] = zscore(out["spread"], self.z_window)
        z = out["spread_z"]
        prev = z.shift(1)

        # Suppress the unsettled warm-up window, where the covariance hasn't converged.
        settled = np.arange(len(out)) >= self.warmup
        gate = settled
        if self.require_cointegration:
            res = engle_granger(y, x, alpha=self.coint_alpha)
            gate = settled & bool(res.cointegrated)

        out["entry"] = (((prev <= -self.entry_z) & (z > -self.entry_z)) & gate).astype(int)
        out["exit"] = (z.abs() <= self.exit_z).astype(int)
        # Conviction grows with how stretched the spread was; capped to [0, 1].
        out["signal_strength"] = (prev.abs() / (2.0 * self.entry_z)).clip(0.0, 1.0).fillna(0.0)
        out["size_hint"] = 1.0
        out["atr"] = (out["spread"].rolling(self.warmup).std() * out["close"]).bfill().fillna(out["close"] * 0.02)
        return out
