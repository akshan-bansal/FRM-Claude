"""Oversold entry — Variant #4: add to a trend position on an oversold pullback.

The mirror of Variant #3 (:mod:`signals.overbought_exit`). V1-V3 give up return by taking profit
early; V4 wins some of it back by buying dips inside an up-trend. Role (user decision 2026-09-18):
**trend pullbacks only.** It fires for a held trend-family position whose strategy signal is still
on (the trend filter), when the last completed bar shows:

* close below the lower Bollinger band (``bb_window``, ``bb_std``), or
* RSI(``rsi_window``) at or below ``rsi_level``.

It never opens a position on its own (a trend strategy's own entry already buys while its trend is
up) and never catches a falling knife: no buys while the trend is off. Guards, enforced by the live
loop: an ATR stop on every V4 tranche, at most one V4 entry per symbol per ``cooldown_bars`` bars,
normal sizing, and the Router.

Parameters are the textbook defaults (20/2 sd, 14/30), the centre of the Phase 3 grid, fixed a
priori. **Not walk-forward calibrated.** Off by default everywhere.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .indicators import bollinger, rsi


@dataclass(frozen=True)
class OversoldEntry:
    bb_window: int = 20
    bb_std: float = 2.0
    rsi_window: int = 14
    rsi_level: float = 30.0
    cooldown_bars: int = 5

    def __post_init__(self) -> None:
        if self.bb_window < 2 or self.rsi_window < 2:
            raise ValueError("bb_window and rsi_window must be >= 2")
        if not (0.0 < self.rsi_level < 50.0):
            raise ValueError(f"rsi_level must be in (0, 50), got {self.rsi_level}")
        if self.cooldown_bars < 1:
            raise ValueError("cooldown_bars must be >= 1")

    def fired(self, df: pd.DataFrame) -> pd.DataFrame:
        """Boolean frame with ``close_below_lower_bb`` and ``rsi_oversold`` columns."""
        if "close" not in df.columns:
            return pd.DataFrame(index=df.index)
        close = df["close"].astype(float)
        lower = bollinger(close, self.bb_window, self.bb_std)["bb_lower"]
        r = rsi(close, self.rsi_window)
        # rsi() is NaN when the average loss is zero: a run with no down days is the least
        # oversold case, so it never fires (NaN compares False). Warm-up NaNs never fire either.
        return pd.DataFrame({
            "close_below_lower_bb": (close < lower).fillna(False).astype(bool),
            "rsi_oversold": (r <= self.rsi_level).fillna(False).astype(bool),
        }, index=df.index)

    @staticmethod
    def trend_on(signals: pd.DataFrame, row: int = -2) -> bool:
        """The strategy's own trend filter on the given (completed) bar: entry on and exit off."""
        if signals.empty or len(signals) < abs(row):
            return False
        r = signals.iloc[row]
        return int(r.get("entry", 0) or 0) == 1 and int(r.get("exit", 0) or 0) == 0
