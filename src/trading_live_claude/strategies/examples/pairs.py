"""Simple pairs trade z-score reversion.

This is a *single-leg representation* — the strategy emits long-only signals
on the spread between two symbols. The backtest engine treats the entry as
"long symbol A vs short symbol B" via the size_hint field. Live execution
of a real pair would need a paired router, which is out of scope for v1;
treat this as a research-only strategy until you wire that up.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ...signals.indicators import zscore
from ..base import Strategy, StrategyContext


class PairsZScore(Strategy):
    name = "pairs"
    description = "Z-score spread reversion (research-only; needs paired router for live)"

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
