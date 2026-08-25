from __future__ import annotations

import pandas as pd

from ...signals.indicators import atr, bollinger
from ..base import Strategy, StrategyContext


class BollingerMeanRevert(Strategy):
    """Buy at lower band, exit at mid band. Long-only."""

    name = "bollinger"
    description = "Mean-revert at 2 sigma Bollinger bands"
    stop_atr_mult: float | None = 3.0  # floor the falling-knife downside on a dip buy

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
        out["entry"] = ((prev_close < out["bb_lower"].shift(1)) & (out["close"] >= out["bb_lower"])).astype(int)
        out["exit"] = (out["close"] >= out["bb_mid"]).astype(int)
        out["size_hint"] = 1.0
        # Graded conviction: depth of the dip, mid->lower band mapped to [0, 1].
        span = (out["bb_mid"] - out["bb_lower"]).replace(0.0, pd.NA)
        out["signal_strength"] = ((out["bb_mid"] - out["close"]) / span).clip(0.0, 1.0).fillna(0.0)
        return out
