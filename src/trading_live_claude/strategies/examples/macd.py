from __future__ import annotations

import pandas as pd

from ...signals.indicators import atr, macd
from ..base import Strategy, StrategyContext


class MacdSignalCross(Strategy):
    """MACD line / signal-line crossover. Long-only."""

    name = "macd"
    description = "MACD line crosses signal line"

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9, allow_short: bool = True, atr_window: int = 14) -> None:
        super().__init__(fast=fast, slow=slow, signal=signal, allow_short=allow_short, atr_window=atr_window)
        self.fast = fast
        self.slow = slow
        self.signal = signal
        self.allow_short = allow_short
        self.atr_window = atr_window

    def required_history_bars(self) -> int:
        return max(self.slow * 4, 150)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        m = macd(out["close"], self.fast, self.slow, self.signal)
        out = pd.concat([out, m], axis=1)
        out["atr"] = atr(out, self.atr_window)
        cross_up = (out["macd"] > out["signal"]) & (out["macd"].shift(1) <= out["signal"].shift(1))
        cross_dn = (out["macd"] < out["signal"]) & (out["macd"].shift(1) >= out["signal"].shift(1))
        out["entry"] = cross_up.astype(int)
        out["exit"] = cross_dn.astype(int)
        if self.allow_short:  # symmetric MACD short below the signal line
            out["short_entry"] = cross_dn.astype(int)
            out["short_exit"] = cross_up.astype(int)
        out["size_hint"] = 1.0
        return out
