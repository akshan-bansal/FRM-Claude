"""Calendar/seasonality strategies (multiple methods).

These are driven purely by the calendar (from the ``time`` column), never by future
prices, so they are structurally lookahead-free. Long-only, single-symbol; each
carries an ``atr`` column for the sizer and a binary ``signal_strength``.
"""
from __future__ import annotations

import pandas as pd

from ...signals.indicators import atr
from ..base import Strategy, StrategyContext


def _times(out: pd.DataFrame) -> pd.Series:
    if "time" not in out.columns:
        raise ValueError("seasonality strategies require a 'time' column")
    return pd.to_datetime(out["time"])


class TurnOfMonth(Strategy):
    """Turn-of-the-month effect: hold across the last/first calendar days of a month.

    Entry: entering the window (last ``pre`` days or first ``post`` days of a month).
    Exit:  leaving the window.
    """

    name = "turn_of_month"
    description = "Turn-of-month seasonal window"

    def __init__(self, pre: int = 3, post: int = 3, atr_window: int = 14) -> None:
        super().__init__(pre=pre, post=post, atr_window=atr_window)
        self.pre = pre
        self.post = post
        self.atr_window = atr_window

    def required_history_bars(self) -> int:
        return 60

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        t = _times(out)
        dom = t.dt.day
        dim = t.dt.days_in_month
        in_win = (dom >= (dim - self.pre + 1)) | (dom <= self.post)
        out["atr"] = atr(out, self.atr_window)
        prev = in_win.shift(1, fill_value=False)
        out["entry"] = (in_win & ~prev).astype(int)
        out["exit"] = (~in_win & prev).astype(int)
        out["signal_strength"] = in_win.astype(float)
        return out


class DayOfWeek(Strategy):
    """Day-of-week effect: enter on ``entry_dow``, exit on ``exit_dow`` (Mon=0)."""

    name = "day_of_week"
    description = "Day-of-week seasonal hold"

    def __init__(self, entry_dow: int = 0, exit_dow: int = 4, atr_window: int = 14) -> None:
        super().__init__(entry_dow=entry_dow, exit_dow=exit_dow, atr_window=atr_window)
        self.entry_dow = entry_dow
        self.exit_dow = exit_dow
        self.atr_window = atr_window

    def required_history_bars(self) -> int:
        return 60

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        dow = _times(out).dt.dayofweek
        out["atr"] = atr(out, self.atr_window)
        out["entry"] = (dow == self.entry_dow).astype(int)
        out["exit"] = (dow == self.exit_dow).astype(int)
        out["signal_strength"] = (dow == self.entry_dow).astype(float)
        return out


class MonthOfYear(Strategy):
    """Month-of-year effect ('sell in May'): long during favorable months only.

    Entry: entering a favorable month. Exit: leaving the favorable set. Default
    favorable months are Nov-Apr (the historically stronger half-year).
    """

    name = "month_of_year"
    description = "Favorable-months seasonal hold"

    def __init__(self, months: tuple[int, ...] = (11, 12, 1, 2, 3, 4), atr_window: int = 14) -> None:
        super().__init__(months=months, atr_window=atr_window)
        self.months = months
        self.atr_window = atr_window

    def required_history_bars(self) -> int:
        return 60

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        month = _times(out).dt.month
        favorable = month.isin(self.months)
        out["atr"] = atr(out, self.atr_window)
        prev = favorable.shift(1, fill_value=False)
        out["entry"] = (favorable & ~prev).astype(int)
        out["exit"] = (~favorable & prev).astype(int)
        out["signal_strength"] = favorable.astype(float)
        return out
