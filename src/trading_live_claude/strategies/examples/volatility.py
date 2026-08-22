"""Volatility-based strategies (multiple methods).

Long-only, single-symbol, no-lookahead. Each emits a graded ``signal_strength``.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ...signals.indicators import atr, bollinger, ema, sma
from ..base import Strategy, StrategyContext


class AtrChannel(Strategy):
    """Keltner-style ATR channel breakout.

    Entry: close above the upper channel (EMA + k*ATR). Exit: close back below the EMA.
    """

    name = "atr_channel"
    description = "ATR/Keltner channel breakout"

    def __init__(self, ema_window: int = 20, k: float = 2.0, atr_window: int = 14) -> None:
        super().__init__(ema_window=ema_window, k=k, atr_window=atr_window)
        self.ema_window = ema_window
        self.k = k
        self.atr_window = atr_window

    def required_history_bars(self) -> int:
        return max(self.ema_window * 6, 120)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        mid = ema(out["close"], self.ema_window)
        a = atr(out, self.atr_window)
        out["atr"] = a
        upper = (mid + self.k * a).shift(1)
        out["entry"] = (out["close"] > upper).astype(int)
        out["exit"] = (out["close"] < mid.shift(1)).astype(int)
        out["signal_strength"] = ((out["close"] - upper) / (self.k * a)).clip(0.0, 1.0).fillna(0.0)
        return out


class BbWidthSqueeze(Strategy):
    """Bollinger-bandwidth squeeze then expansion breakout.

    Entry: bandwidth was in its low ``squeeze_q`` quantile AND close breaks the upper band.
    Exit:  close back below the middle band.
    """

    name = "bbwidth_squeeze"
    description = "Bollinger bandwidth squeeze breakout"

    def __init__(
        self, window: int = 20, n_std: float = 2.0, rank_window: int = 120, squeeze_q: float = 0.25, atr_window: int = 14
    ) -> None:
        super().__init__(
            window=window, n_std=n_std, rank_window=rank_window, squeeze_q=squeeze_q, atr_window=atr_window
        )
        self.window = window
        self.n_std = n_std
        self.rank_window = rank_window
        self.squeeze_q = squeeze_q
        self.atr_window = atr_window

    def required_history_bars(self) -> int:
        return max(self.rank_window + self.window, 160)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        b = bollinger(out["close"], self.window, self.n_std)
        width = (b["bb_upper"] - b["bb_lower"]) / b["bb_mid"]
        low_th = width.rolling(self.rank_window, min_periods=self.window).quantile(self.squeeze_q)
        out["atr"] = atr(out, self.atr_window)
        squeezed = width.shift(1) <= low_th.shift(1)
        out["entry"] = (squeezed & (out["close"] > b["bb_upper"].shift(1))).astype(int)
        out["exit"] = (out["close"] < b["bb_mid"]).astype(int)
        out["signal_strength"] = out["entry"].astype(float)
        return out


class VolTarget(Strategy):
    """Risk-on when realized volatility is calm and price trends up.

    Entry: annualized realized vol < ``target_vol`` AND close above its trend SMA.
    Exit:  vol spikes past ``target_vol * exit_mult`` OR close falls below the trend.
    """

    name = "vol_target"
    description = "Long in calm up-trending vol regimes"

    def __init__(
        self, vol_window: int = 20, target_vol: float = 0.20, exit_mult: float = 1.5, trend_window: int = 100, atr_window: int = 14
    ) -> None:
        super().__init__(
            vol_window=vol_window, target_vol=target_vol, exit_mult=exit_mult, trend_window=trend_window, atr_window=atr_window
        )
        self.vol_window = vol_window
        self.target_vol = target_vol
        self.exit_mult = exit_mult
        self.trend_window = trend_window
        self.atr_window = atr_window

    def required_history_bars(self) -> int:
        return max(self.trend_window + self.vol_window, 130)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        rvol = out["close"].pct_change().rolling(self.vol_window).std() * np.sqrt(252.0)
        trend = sma(out["close"], self.trend_window)
        out["atr"] = atr(out, self.atr_window)
        out["entry"] = ((rvol < self.target_vol) & (out["close"] > trend)).astype(int)
        out["exit"] = ((rvol > self.target_vol * self.exit_mult) | (out["close"] < trend)).astype(int)
        out["signal_strength"] = (self.target_vol / rvol).clip(0.0, 1.0).fillna(0.0)
        return out
