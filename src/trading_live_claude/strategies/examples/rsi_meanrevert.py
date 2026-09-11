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
    # Emits both entry columns (2026-09-10). ``entry`` fires on the fresh cross where
    # RSI crosses UP through ``oversold``. ``entry_level`` fires whenever the current
    # RSI is at or below ``oversold`` — the setup is currently eligible even without
    # a fresh cross. LiveMonitor's open-position guard prevents re-entry while a
    # position is held. NOTE: level-triggered mean-reversion can buy into an
    # ongoing drop; the exit rule (RSI reverts to neutral) is what defines the
    # thesis and the time_stop kick-out on the base risk contract still applies.
    supports_level_trigger: bool = True

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
        # Level trigger: currently in the oversold zone (no fresh-cross requirement).
        # fillna guards warm-up bars where RSI is NaN before the window fills.
        out["entry_level"] = (out["rsi"] <= self.oversold).fillna(False).astype(int)
        out["exit"] = ((prev <= self.neutral) & (out["rsi"] > self.neutral)).astype(int)
        out["size_hint"] = 1.0
        # Graded conviction: how far below the neutral line RSI sits (more oversold = stronger).
        out["signal_strength"] = ((self.neutral - out["rsi"]) / self.neutral).clip(0.0, 1.0).fillna(0.0)
        return out
