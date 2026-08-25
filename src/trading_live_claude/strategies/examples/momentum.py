"""Advanced momentum strategies (multiple methods).

Long-only, single-symbol, no-lookahead. Each emits a graded ``signal_strength``.
"""
from __future__ import annotations

import pandas as pd

from ...signals.indicators import atr, sma
from ..base import Strategy, StrategyContext


class TsMomentum(Strategy):
    """Time-series (absolute) momentum via rate-of-change.

    Entry: ``lookback``-bar return > ``threshold``.
    Exit:  return turns negative.
    """

    name = "ts_momentum"
    description = "Time-series momentum (ROC sign)"

    def __init__(self, lookback: int = 126, threshold: float = 0.0, scale: float = 0.25, atr_window: int = 14) -> None:
        super().__init__(lookback=lookback, threshold=threshold, scale=scale, atr_window=atr_window)
        self.lookback = lookback
        self.threshold = threshold
        self.scale = scale
        self.atr_window = atr_window

    def required_history_bars(self) -> int:
        return max(self.lookback * 2, 120)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        roc = out["close"] / out["close"].shift(self.lookback) - 1.0
        out["atr"] = atr(out, self.atr_window)
        out["entry"] = (roc > self.threshold).astype(int)
        out["exit"] = (roc < 0.0).astype(int)
        out["signal_strength"] = (roc / self.scale).clip(0.0, 1.0).fillna(0.0)
        return out


class DualMa(Strategy):
    """Dual simple-moving-average trend filter (SMA, distinct from EMA crossover).

    Entry: fast SMA above slow SMA. Exit: fast SMA below slow SMA.
    """

    name = "dual_ma"
    description = "Fast/slow SMA trend crossover"

    def __init__(self, fast: int = 50, slow: int = 200, scale: float = 0.1, band: float = 0.01, allow_short: bool = True, atr_window: int = 14) -> None:
        super().__init__(fast=fast, slow=slow, scale=scale, band=band, allow_short=allow_short, atr_window=atr_window)
        self.fast = fast
        self.slow = slow
        self.scale = scale
        self.band = band
        self.allow_short = allow_short
        self.atr_window = atr_window

    def required_history_bars(self) -> int:
        return max(self.slow * 2, 120)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        fast = sma(out["close"], self.fast)
        slow = sma(out["close"], self.slow)
        out["atr"] = atr(out, self.atr_window)
        # Hysteresis band: enter above slow*(1+band), exit below slow*(1-band) — the dead
        # zone between them stops fast~slow chop from whipsawing in and out every bar.
        long_ok = fast > slow * (1.0 + self.band)
        short_ok = fast < slow * (1.0 - self.band)
        out["entry"] = long_ok.astype(int)
        out["exit"] = short_ok.astype(int)
        if self.allow_short:
            out["short_entry"] = short_ok.astype(int)
            out["short_exit"] = long_ok.astype(int)
        gap = (fast - slow) / slow
        out["signal_strength"] = (gap / self.scale).clip(0.0, 1.0).fillna(0.0)
        return out


class High52wBreakout(Strategy):
    """Breakout to a new rolling high (52-week style).

    Entry: close makes a new ``high_window`` high (vs the prior window).
    Exit:  close falls below the ``exit_window`` low.
    """

    name = "high_52w_breakout"
    description = "New rolling-high breakout, long-only"

    def __init__(self, high_window: int = 252, exit_window: int = 63, atr_window: int = 14) -> None:
        super().__init__(high_window=high_window, exit_window=exit_window, atr_window=atr_window)
        self.high_window = high_window
        self.exit_window = exit_window
        self.atr_window = atr_window

    def required_history_bars(self) -> int:
        return max(self.high_window + 10, 260)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        prior_high = out["close"].rolling(self.high_window, min_periods=self.high_window).max().shift(1)
        prior_low = out["close"].rolling(self.exit_window, min_periods=self.exit_window).min().shift(1)
        out["atr"] = atr(out, self.atr_window)
        out["entry"] = (out["close"] >= prior_high).astype(int)
        out["exit"] = (out["close"] < prior_low).astype(int)
        out["signal_strength"] = ((out["close"] / prior_high - 1.0) / 0.05).clip(0.0, 1.0).fillna(0.0)
        return out
