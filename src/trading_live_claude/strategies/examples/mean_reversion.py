"""Advanced mean-reversion strategies (multiple methods).

All long-only, single-symbol, no-lookahead. Each emits a graded ``signal_strength``
in [0, 1] so the scoring/precision stage can weight conviction.
"""
from __future__ import annotations

import pandas as pd

from ...signals.indicators import atr, bollinger, rsi, sma, zscore
from ..base import Strategy, StrategyContext


class Rsi2Connors(Strategy):
    """Connors RSI(2) pullback: buy short-term oversold while in an uptrend.

    Entry: RSI(2) < ``entry_th`` AND close above the ``trend_window`` SMA.
    Exit:  close back above the short ``exit_window`` SMA.
    """

    name = "rsi2_connors"
    description = "RSI(2) pullback above the 200-SMA (Connors)"
    stop_atr_mult: float | None = 3.0

    def __init__(
        self,
        rsi_window: int = 2,
        entry_th: float = 10.0,
        trend_window: int = 200,
        exit_window: int = 5,
        atr_window: int = 14,
    ) -> None:
        super().__init__(
            rsi_window=rsi_window, entry_th=entry_th, trend_window=trend_window,
            exit_window=exit_window, atr_window=atr_window,
        )
        self.rsi_window = rsi_window
        self.entry_th = entry_th
        self.trend_window = trend_window
        self.exit_window = exit_window
        self.atr_window = atr_window

    def required_history_bars(self) -> int:
        return max(self.trend_window + 10, 210)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        r = rsi(out["close"], self.rsi_window)
        trend = sma(out["close"], self.trend_window)
        exit_ma = sma(out["close"], self.exit_window)
        out["atr"] = atr(out, self.atr_window)
        out["entry"] = ((r < self.entry_th) & (out["close"] > trend)).astype(int)
        out["exit"] = (out["close"] > exit_ma).astype(int)
        out["signal_strength"] = ((self.entry_th - r) / self.entry_th).clip(0.0, 1.0).fillna(0.0)
        return out


class ZScoreOU(Strategy):
    """Ornstein-Uhlenbeck-style z-score reversion.

    Entry: rolling z-score of price < ``-entry_z`` (stretched below the mean).
    Exit:  z-score reverts above ``exit_z`` (default the mean, 0).
    """

    name = "zscore_ou"
    description = "Rolling z-score mean reversion"
    stop_atr_mult: float | None = 3.0

    def __init__(self, window: int = 20, entry_z: float = 2.0, exit_z: float = 0.0, atr_window: int = 14) -> None:
        super().__init__(window=window, entry_z=entry_z, exit_z=exit_z, atr_window=atr_window)
        self.window = window
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.atr_window = atr_window

    def required_history_bars(self) -> int:
        return max(self.window * 6, 120)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        z = zscore(out["close"], self.window)
        out["atr"] = atr(out, self.atr_window)
        out["entry"] = (z < -self.entry_z).astype(int)
        out["exit"] = (z > self.exit_z).astype(int)
        out["signal_strength"] = (-z / (self.entry_z * 1.5)).clip(0.0, 1.0).fillna(0.0)
        return out


class BbRsiCombo(Strategy):
    """Confluence of a Bollinger lower-band tag AND an oversold RSI.

    Entry: close below the lower band AND RSI < ``rsi_th``.
    Exit:  close back above the middle band.
    """

    name = "bb_rsi_combo"
    description = "Bollinger lower-band tag confirmed by oversold RSI"
    stop_atr_mult: float | None = 3.0

    def __init__(
        self, window: int = 20, n_std: float = 2.0, rsi_window: int = 14, rsi_th: float = 40.0, atr_window: int = 14
    ) -> None:
        super().__init__(window=window, n_std=n_std, rsi_window=rsi_window, rsi_th=rsi_th, atr_window=atr_window)
        self.window = window
        self.n_std = n_std
        self.rsi_window = rsi_window
        self.rsi_th = rsi_th
        self.atr_window = atr_window

    def required_history_bars(self) -> int:
        return max(self.window * 6, 120)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        b = bollinger(out["close"], self.window, self.n_std)
        out = pd.concat([out, b], axis=1)
        r = rsi(out["close"], self.rsi_window)
        out["atr"] = atr(out, self.atr_window)
        out["entry"] = ((out["close"] < out["bb_lower"]) & (r < self.rsi_th)).astype(int)
        out["exit"] = (out["close"] > out["bb_mid"]).astype(int)
        band = (out["bb_upper"] - out["bb_lower"]).replace(0, pd.NA)
        depth = ((out["bb_lower"] - out["close"]) / band).clip(0.0, 1.0)
        rsi_s = ((self.rsi_th - r) / self.rsi_th).clip(0.0, 1.0)
        out["signal_strength"] = ((depth + rsi_s) / 2.0).astype(float).fillna(0.0)
        return out
