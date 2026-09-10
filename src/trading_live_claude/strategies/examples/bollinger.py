from __future__ import annotations

import pandas as pd

from ...signals.indicators import atr, bollinger
from ..base import Strategy, StrategyContext


class BollingerMeanRevert(Strategy):
    """Buy at lower band, exit at mid band. Long-only."""

    name = "bollinger"
    description = "Mean-revert at 2 sigma Bollinger bands"
    # A 15-bar time stop tested as a clean win (better score, drawdown, and return): a
    # dip that hasn't reverted in 15 bars is a stale thesis. Fixed ATR stops hurt here.
    time_stop_bars: int | None = 15
    # Emits both entry columns (2026-09-09): ``entry`` fires on the fresh cross where
    # close crosses back up through the lower band; ``entry_level`` fires whenever the
    # current close sits at or below the lower band. Level covers the case where the
    # monitor starts while a symbol is already oversold and the event-triggered path
    # has no fresh cross to point at.
    supports_level_trigger: bool = True

    def __init__(self, window: int = 20, n_std: float = 2.0, atr_window: int = 14) -> None:
        super().__init__(window=window, n_std=n_std, atr_window=atr_window)
        self.window = window
        self.n_std = n_std
        self.atr_window = atr_window

    def required_history_bars(self) -> int:
        return max(self.window * 6, 120)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        b = bollinger(out["close"], self.window, self.n_std)
        out = pd.concat([out, b], axis=1)
        out["atr"] = atr(out, self.atr_window)
        prev_close = out["close"].shift(1)
        # Event trigger — fresh cross back up through the lower band (yesterday closed
        # below, today closes at-or-above). Fires once on the crossing bar.
        out["entry"] = ((prev_close < out["bb_lower"].shift(1)) & (out["close"] >= out["bb_lower"])).astype(int)
        # Level trigger — state currently satisfies the entry condition (close at or
        # below the lower band). Fires every bar the condition holds. LiveMonitor's
        # open-position guard suppresses re-entry when a position is already open.
        out["entry_level"] = (out["close"] <= out["bb_lower"]).astype(int)
        out["exit"] = (out["close"] >= out["bb_mid"]).astype(int)
        out["size_hint"] = 1.0
        # Graded conviction: depth of the dip, mid->lower band mapped to [0, 1].
        span = (out["bb_mid"] - out["bb_lower"]).replace(0.0, pd.NA)
        out["signal_strength"] = ((out["bb_mid"] - out["close"]) / span).clip(0.0, 1.0).fillna(0.0)
        return out
