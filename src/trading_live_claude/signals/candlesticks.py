"""Vectorized candlestick pattern library — 40 patterns.

Each pattern is a pure function ``pattern(df) -> pd.Series[bool]`` over an OHLC frame
(columns open/high/low/close). All detection is vectorized and **no-lookahead**:
multi-bar patterns reference only *past* bars via positive ``.shift(k)``, never the
future. Trend context (needed to tell e.g. a hammer from a hanging man) is taken from
prior closes only.

``CANDLESTICK_PATTERNS`` maps name -> function; ``detect_all(df)`` returns a bool
DataFrame of every pattern, and these feed the Strategy layer via
``strategies.candlestick.CandlestickStrategy``.
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

# Shape thresholds (fractions of the bar's high-low range).
_DOJI = 0.1        # body <= 10% of range → doji
_SMALL = 0.30      # "small" body
_LONG_SHADOW = 2.0  # long shadow >= 2x body
_TINY = 0.1        # negligible shadow (<= 10% of range)


class _F:
    """Precomputed candle geometry for a frame (all pandas Series)."""

    def __init__(self, df: pd.DataFrame) -> None:
        self.o = df["open"].astype(float)
        self.h = df["high"].astype(float)
        self.c = df["close"].astype(float)
        self.l = df["low"].astype(float)
        self.rng = (self.h - self.l).replace(0.0, np.nan)
        self.body = (self.c - self.o).abs()
        self.top = np.maximum(self.o, self.c)
        self.bot = np.minimum(self.o, self.c)
        self.upper = self.h - self.top
        self.lower = self.bot - self.l
        self.bull = self.c > self.o
        self.bear = self.c < self.o
        self.body_frac = self.body / self.rng
        self.mid = (self.o + self.c) / 2.0


def _b(s: pd.Series) -> pd.Series:
    """Coerce a possibly-NaN boolean expression to a clean bool Series."""
    return s.fillna(False).astype(bool)


def _prior_down(c: pd.Series, n: int = 3) -> pd.Series:
    return c.shift(1) < c.shift(1 + n)


def _prior_up(c: pd.Series, n: int = 3) -> pd.Series:
    return c.shift(1) > c.shift(1 + n)


# ----- single-bar patterns ---------------------------------------------------


def doji(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(f.body_frac <= _DOJI)


def dragonfly_doji(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b((f.body_frac <= _DOJI) & (f.upper <= _TINY * f.rng) & (f.lower >= 0.6 * f.rng))


def gravestone_doji(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b((f.body_frac <= _DOJI) & (f.lower <= _TINY * f.rng) & (f.upper >= 0.6 * f.rng))


def long_legged_doji(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b((f.body_frac <= _DOJI) & (f.upper >= 0.3 * f.rng) & (f.lower >= 0.3 * f.rng))


def _hammer_shape(f: _F) -> pd.Series:
    return (f.lower >= _LONG_SHADOW * f.body) & (f.upper <= f.body) & (f.body_frac <= _SMALL)


def hammer(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(_hammer_shape(f) & _prior_down(f.c))


def hanging_man(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(_hammer_shape(f) & _prior_up(f.c))


def _inv_hammer_shape(f: _F) -> pd.Series:
    return (f.upper >= _LONG_SHADOW * f.body) & (f.lower <= f.body) & (f.body_frac <= _SMALL)


def inverted_hammer(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(_inv_hammer_shape(f) & _prior_down(f.c))


def shooting_star(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(_inv_hammer_shape(f) & _prior_up(f.c))


def spinning_top(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b((f.body_frac <= _SMALL) & (f.body_frac > _DOJI) & (f.upper >= f.body) & (f.lower >= f.body))


def marubozu_bull(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(f.bull & (f.upper <= _TINY * f.rng) & (f.lower <= _TINY * f.rng) & (f.body_frac >= 0.8))


def marubozu_bear(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(f.bear & (f.upper <= _TINY * f.rng) & (f.lower <= _TINY * f.rng) & (f.body_frac >= 0.8))


def belt_hold_bull(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(f.bull & (f.o - f.l <= _TINY * f.rng) & (f.body_frac >= 0.6) & _prior_down(f.c))


def belt_hold_bear(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(f.bear & (f.h - f.o <= _TINY * f.rng) & (f.body_frac >= 0.6) & _prior_up(f.c))


def high_wave(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b((f.body_frac <= _SMALL) & (f.upper >= f.rng * 0.3) & (f.lower >= f.rng * 0.3))


# ----- two-bar patterns ------------------------------------------------------


def bullish_engulfing(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    prev_bear = f.bear.shift(1).fillna(False)
    return _b(prev_bear & f.bull & (f.c >= f.o.shift(1)) & (f.o <= f.c.shift(1)))


def bearish_engulfing(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    prev_bull = f.bull.shift(1).fillna(False)
    return _b(prev_bull & f.bear & (f.o >= f.c.shift(1)) & (f.c <= f.o.shift(1)))


def bullish_harami(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    prev_bear = f.bear.shift(1).fillna(False)
    return _b(prev_bear & f.bull & (f.top <= f.o.shift(1)) & (f.bot >= f.c.shift(1)))


def bearish_harami(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    prev_bull = f.bull.shift(1).fillna(False)
    return _b(prev_bull & f.bear & (f.top <= f.c.shift(1)) & (f.bot >= f.o.shift(1)))


def harami_cross(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    is_doji = f.body_frac <= _DOJI
    inside = (f.top <= np.maximum(f.o.shift(1), f.c.shift(1))) & (f.bot >= np.minimum(f.o.shift(1), f.c.shift(1)))
    return _b(is_doji & inside)


def piercing_line(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    prev_bear = f.bear.shift(1).fillna(False)
    return _b(prev_bear & f.bull & (f.o < f.l.shift(1)) & (f.c > f.mid.shift(1)) & (f.c < f.o.shift(1)))


def dark_cloud_cover(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    prev_bull = f.bull.shift(1).fillna(False)
    return _b(prev_bull & f.bear & (f.o > f.h.shift(1)) & (f.c < f.mid.shift(1)) & (f.c > f.o.shift(1)))


def tweezer_bottom(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    same_low = (f.l - f.l.shift(1)).abs() <= _TINY * f.rng
    return _b(same_low & f.bear.shift(1).fillna(False) & f.bull & _prior_down(f.c))


def tweezer_top(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    same_high = (f.h - f.h.shift(1)).abs() <= _TINY * f.rng
    return _b(same_high & f.bull.shift(1).fillna(False) & f.bear & _prior_up(f.c))


def bullish_kicker(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(f.bear.shift(1).fillna(False) & f.bull & (f.o > f.o.shift(1)) & (f.body_frac >= 0.6))


def bearish_kicker(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(f.bull.shift(1).fillna(False) & f.bear & (f.o < f.o.shift(1)) & (f.body_frac >= 0.6))


def on_neck(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(f.bear.shift(1).fillna(False) & f.bull & (f.o < f.l.shift(1)) & ((f.c - f.l.shift(1)).abs() <= _TINY * f.rng))


def matching_low(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(f.bear.shift(1).fillna(False) & f.bear & ((f.c - f.c.shift(1)).abs() <= _TINY * f.rng) & _prior_down(f.c))


# ----- three-bar patterns ----------------------------------------------------


def morning_star(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    first_bear = f.bear.shift(2).fillna(False) & (f.body_frac.shift(2) >= 0.5)
    small_mid = f.body_frac.shift(1) <= _SMALL
    gap_down = np.maximum(f.o.shift(1), f.c.shift(1)) < f.c.shift(2)
    third_up = f.bull & (f.c > f.mid.shift(2))
    return _b(first_bear & small_mid & gap_down & third_up)


def evening_star(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    first_bull = f.bull.shift(2).fillna(False) & (f.body_frac.shift(2) >= 0.5)
    small_mid = f.body_frac.shift(1) <= _SMALL
    gap_up = np.minimum(f.o.shift(1), f.c.shift(1)) > f.c.shift(2)
    third_down = f.bear & (f.c < f.mid.shift(2))
    return _b(first_bull & small_mid & gap_up & third_down)


def morning_doji_star(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(morning_star(df) & (f.body_frac.shift(1) <= _DOJI))


def evening_doji_star(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(evening_star(df) & (f.body_frac.shift(1) <= _DOJI))


def three_white_soldiers(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    b = f.bull & f.bull.shift(1).fillna(False) & f.bull.shift(2).fillna(False)
    rising = (f.c > f.c.shift(1)) & (f.c.shift(1) > f.c.shift(2))
    opens_in = (f.o < f.c.shift(1)) & (f.o > f.o.shift(1))
    big = (f.body_frac >= 0.5) & (f.body_frac.shift(1) >= 0.5)
    return _b(b & rising & opens_in & big)


def three_black_crows(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    b = f.bear & f.bear.shift(1).fillna(False) & f.bear.shift(2).fillna(False)
    falling = (f.c < f.c.shift(1)) & (f.c.shift(1) < f.c.shift(2))
    opens_in = (f.o > f.c.shift(1)) & (f.o < f.o.shift(1))
    big = (f.body_frac >= 0.5) & (f.body_frac.shift(1) >= 0.5)
    return _b(b & falling & opens_in & big)


def three_inside_up(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(bullish_harami(df).shift(1).fillna(False) & f.bull & (f.c > f.c.shift(1)))


def three_inside_down(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(bearish_harami(df).shift(1).fillna(False) & f.bear & (f.c < f.c.shift(1)))


def three_outside_up(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(bullish_engulfing(df).shift(1).fillna(False) & f.bull & (f.c > f.c.shift(1)))


def three_outside_down(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    return _b(bearish_engulfing(df).shift(1).fillna(False) & f.bear & (f.c < f.c.shift(1)))


def abandoned_baby_bull(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    doji_mid = f.body_frac.shift(1) <= _DOJI
    gap1 = f.h.shift(1) < f.l.shift(2)   # doji gaps below prior bar
    gap2 = f.l > f.h.shift(1)            # third bar gaps above doji
    return _b(f.bear.shift(2).fillna(False) & doji_mid & gap1 & gap2 & f.bull)


def abandoned_baby_bear(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    doji_mid = f.body_frac.shift(1) <= _DOJI
    gap1 = f.l.shift(1) > f.h.shift(2)
    gap2 = f.h < f.l.shift(1)
    return _b(f.bull.shift(2).fillna(False) & doji_mid & gap1 & gap2 & f.bear)


def tri_star(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    d = f.body_frac <= _DOJI
    return _b(d & d.shift(1).fillna(False) & d.shift(2).fillna(False))


def rising_three_methods(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    long1 = f.bull.shift(4).fillna(False) & (f.body_frac.shift(4) >= 0.5)
    small = (f.body_frac.shift(3) <= _SMALL) & (f.body_frac.shift(2) <= _SMALL) & (f.body_frac.shift(1) <= _SMALL)
    contained = (f.h.shift(3) <= f.h.shift(4)) & (f.l.shift(1) >= f.l.shift(4))
    breakout = f.bull & (f.c > f.c.shift(4))
    return _b(long1 & small & contained & breakout)


def falling_three_methods(df: pd.DataFrame) -> pd.Series:
    f = _F(df)
    long1 = f.bear.shift(4).fillna(False) & (f.body_frac.shift(4) >= 0.5)
    small = (f.body_frac.shift(3) <= _SMALL) & (f.body_frac.shift(2) <= _SMALL) & (f.body_frac.shift(1) <= _SMALL)
    contained = (f.l.shift(3) >= f.l.shift(4)) & (f.h.shift(1) <= f.h.shift(4))
    breakdown = f.bear & (f.c < f.c.shift(4))
    return _b(long1 & small & contained & breakdown)


CANDLESTICK_PATTERNS: dict[str, Callable[[pd.DataFrame], pd.Series]] = {
    "doji": doji,
    "dragonfly_doji": dragonfly_doji,
    "gravestone_doji": gravestone_doji,
    "long_legged_doji": long_legged_doji,
    "hammer": hammer,
    "hanging_man": hanging_man,
    "inverted_hammer": inverted_hammer,
    "shooting_star": shooting_star,
    "spinning_top": spinning_top,
    "marubozu_bull": marubozu_bull,
    "marubozu_bear": marubozu_bear,
    "belt_hold_bull": belt_hold_bull,
    "belt_hold_bear": belt_hold_bear,
    "high_wave": high_wave,
    "bullish_engulfing": bullish_engulfing,
    "bearish_engulfing": bearish_engulfing,
    "bullish_harami": bullish_harami,
    "bearish_harami": bearish_harami,
    "harami_cross": harami_cross,
    "piercing_line": piercing_line,
    "dark_cloud_cover": dark_cloud_cover,
    "tweezer_bottom": tweezer_bottom,
    "tweezer_top": tweezer_top,
    "bullish_kicker": bullish_kicker,
    "bearish_kicker": bearish_kicker,
    "on_neck": on_neck,
    "matching_low": matching_low,
    "morning_star": morning_star,
    "evening_star": evening_star,
    "morning_doji_star": morning_doji_star,
    "evening_doji_star": evening_doji_star,
    "three_white_soldiers": three_white_soldiers,
    "three_black_crows": three_black_crows,
    "three_inside_up": three_inside_up,
    "three_inside_down": three_inside_down,
    "three_outside_up": three_outside_up,
    "three_outside_down": three_outside_down,
    "abandoned_baby_bull": abandoned_baby_bull,
    "abandoned_baby_bear": abandoned_baby_bear,
    "tri_star": tri_star,
    "rising_three_methods": rising_three_methods,
    "falling_three_methods": falling_three_methods,
}

# Patterns that signal a bullish (long-entry) reversal/continuation.
BULLISH_PATTERNS: tuple[str, ...] = (
    "hammer", "inverted_hammer", "dragonfly_doji", "bullish_engulfing", "bullish_harami",
    "piercing_line", "tweezer_bottom", "bullish_kicker", "belt_hold_bull", "matching_low",
    "morning_star", "morning_doji_star", "three_white_soldiers", "three_inside_up",
    "three_outside_up", "abandoned_baby_bull", "rising_three_methods",
)


def detect_all(df: pd.DataFrame) -> pd.DataFrame:
    """Return a bool DataFrame with one column per candlestick pattern."""
    return pd.DataFrame({name: fn(df) for name, fn in CANDLESTICK_PATTERNS.items()}, index=df.index)
