from __future__ import annotations

import pandas as pd

from ...signals.indicators import atr, rsi
from ..base import Strategy, StrategyContext


class RsiMeanRevert(Strategy):
    """RSI mean-reversion. Long-only.

    Entry: RSI crosses up through ``oversold`` (default 30)
    Exit:  RSI crosses up through ``neutral`` (default 50)
    """

    name = "rsi_meanrevert"
    description = "Long when RSI exits oversold; flat when RSI reverts to mean"
    stop_atr_mult: float | None = 3.0  # floor the downside if the bounce fails

    def __init__(self, window: int = 14, oversold: float = 30.0, neutral: float = 50.0, atr_window: int = 14) -> None:
        super().__init__(window=window, oversold=oversold, neutral=neutral, atr_window=atr_window)
        self.window = window
        self.oversold = oversold
        self.neutral = neutral
        self.atr_window = atr_window

    def required_history_bars(self) -> int:
        return max(self.window * 6, 100)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        out["rsi"] = rsi(out["close"], self.window)
        out["atr"] = atr(out, self.atr_window)
        prev = out["rsi"].shift(1)
        out["entry"] = ((prev <= self.oversold) & (out["rsi"] > self.oversold)).astype(int)
        out["exit"] = ((prev <= self.neutral) & (out["rsi"] > self.neutral)).astype(int)
        out["size_hint"] = 1.0
        # Graded conviction: how far below the neutral line RSI sits (more oversold = stronger).
        out["signal_strength"] = ((self.neutral - out["rsi"]) / self.neutral).clip(0.0, 1.0).fillna(0.0)
        return out
