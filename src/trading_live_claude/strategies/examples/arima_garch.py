"""ARIMA + GARCH trend strategy over a ladder of moving averages.

Three signals, combined: a ladder of moving averages of increasing degree sets the trend
backdrop; a rolling ARIMA one-step forecast confirms the *direction* of the next return; and a
rolling GARCH volatility forecast scales conviction and stands the strategy down when the
conditional vol spikes past its own recent median (turbulent regimes chop trend systems up).

Entry: the MA ladder is bullish and ARIMA forecasts a positive next return, in a calm-enough vol
regime. Exit: the ladder turns down or the forecast flips negative. All three inputs are causal.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ...models.timeseries import ma_ladder, rolling_arima_forecast, rolling_garch_vol
from ..base import Strategy, StrategyContext


class ArimaGarchTrend(Strategy):
    name = "arima_garch"
    description = "MA-ladder trend confirmed by ARIMA direction, gated by GARCH volatility regime"

    def __init__(self, trend_threshold: float = 0.2, window: int = 250, arima_order: tuple[int, int, int] = (1, 0, 1),
                 vol_gate: float = 1.5, ladder: tuple[int, ...] = (10, 20, 50, 100, 200)) -> None:
        super().__init__(trend_threshold=trend_threshold, window=window, arima_order=arima_order,
                         vol_gate=vol_gate, ladder=ladder)
        self.trend_threshold = trend_threshold
        self.window = window
        self.arima_order = arima_order
        self.vol_gate = vol_gate
        self.ladder = ladder

    def required_history_bars(self) -> int:
        return max(self.window + 30, max(self.ladder) + 30)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        close = out["close"].astype(float)
        ret = close.pct_change().fillna(0.0).to_numpy()

        trend = ma_ladder(close, self.ladder)
        fc = rolling_arima_forecast(ret, order=self.arima_order, window=self.window)
        vol = rolling_garch_vol(ret, window=self.window)
        out["ma_trend"] = trend
        out["arima_fc"] = fc
        out["garch_vol"] = vol

        fc_s = pd.Series(fc, index=out.index)
        vol_s = pd.Series(vol, index=out.index)
        # Calm regime = conditional vol below vol_gate x its trailing median (both known at t).
        vol_med = vol_s.rolling(self.window, min_periods=20).median()
        calm = (vol_s <= self.vol_gate * vol_med).fillna(False)

        bull = (trend > self.trend_threshold) & (fc_s > 0.0) & calm
        prev_bull = bull.shift(1).fillna(False)
        out["entry"] = (bull & ~prev_bull).astype(int)         # first bar the regime turns bullish
        out["exit"] = ((trend < 0.0) | (fc_s < 0.0)).astype(int)
        # Conviction: forecast size relative to conditional vol, capped.
        conv = (fc_s.abs() / vol_s.replace(0.0, np.nan)).fillna(0.0)
        out["signal_strength"] = (conv / (conv.rolling(self.window, min_periods=20).max())).clip(0.0, 1.0).fillna(0.0)
        out["size_hint"] = 1.0

        prev_close = close.shift(1)
        tr = pd.concat([out["high"] - out["low"], (out["high"] - prev_close).abs(),
                        (out["low"] - prev_close).abs()], axis=1).max(axis=1)
        out["atr"] = tr.rolling(14).mean().bfill().fillna(close * 0.02)
        return out
