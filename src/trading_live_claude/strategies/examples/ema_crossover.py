from __future__ import annotations

import pandas as pd

from ...signals.indicators import atr, ema
from ..base import Strategy, StrategyContext


class EmaCrossover(Strategy):
    """Classic fast/slow EMA crossover. Long-only.

    Entry: ema_fast crosses above ema_slow
    Exit:  ema_fast crosses below ema_slow
    """

    name = "ema_crossover"
    description = "Fast/slow EMA crossover (long; short opt-in)"

    def __init__(self, fast: int = 20, slow: int = 50, allow_short: bool = False, atr_window: int = 14) -> None:
        super().__init__(fast=fast, slow=slow, allow_short=allow_short, atr_window=atr_window)
        self.fast = fast
        self.slow = slow
        self.allow_short = allow_short
        self.atr_window = atr_window

    def required_history_bars(self) -> int:
        return max(self.slow * 3, 150)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        out["ema_fast"] = ema(out["close"], self.fast)
        out["ema_slow"] = ema(out["close"], self.slow)
        out["atr"] = atr(out, self.atr_window)

        cross_up = (out["ema_fast"] > out["ema_slow"]) & (out["ema_fast"].shift(1) <= out["ema_slow"].shift(1))
        cross_dn = (out["ema_fast"] < out["ema_slow"]) & (out["ema_fast"].shift(1) >= out["ema_slow"].shift(1))
        out["entry"] = cross_up.astype(int)
        out["exit"] = cross_dn.astype(int)
        if self.allow_short:  # a downward cross opens a short; an upward cross covers it
            out["short_entry"] = cross_dn.astype(int)
            out["short_exit"] = cross_up.astype(int)
        out["size_hint"] = 1.0
        return out
