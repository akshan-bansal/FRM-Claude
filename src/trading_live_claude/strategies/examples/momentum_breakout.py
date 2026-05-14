from __future__ import annotations

import pandas as pd

from ...signals.indicators import atr, donchian
from ..base import Strategy, StrategyContext


class DonchianBreakout(Strategy):
    """N-day Donchian breakout (a la Turtles).

    Entry: close above the N-bar high (excluding today)
    Exit:  close below the M-bar low (excluding today)
    """

    name = "momentum_breakout"
    description = "Donchian N-bar breakout, long-only"

    def __init__(self, entry_window: int = 55, exit_window: int = 20, atr_window: int = 14) -> None:
        super().__init__(entry_window=entry_window, exit_window=exit_window, atr_window=atr_window)
        self.entry_window = entry_window
        self.exit_window = exit_window
        self.atr_window = atr_window

    def required_history_bars(self) -> int:
        return max(self.entry_window * 3, 200)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        d_in = donchian(out, self.entry_window)
        d_out = donchian(out, self.exit_window)
        out["entry_high"] = d_in["don_upper"].shift(1)
        out["exit_low"] = d_out["don_lower"].shift(1)
        out["atr"] = atr(out, self.atr_window)

        out["entry"] = (out["close"] > out["entry_high"]).astype(int)
        out["exit"] = (out["close"] < out["exit_low"]).astype(int)
        out["size_hint"] = 1.0
        return out
