from __future__ import annotations

import pandas as pd

from trading_live_claude.signals.indicators import sma, zscore
from trading_live_claude.strategies import STRATEGIES
from trading_live_claude.strategies.base import StrategyContext
from trading_live_claude.strategies.examples.momentum import DualMa
from trading_live_claude.strategies.examples.seasonality import TREND_WINDOW

CTX = StrategyContext(symbol="T")


# --- #7 confirmed cross-back entries ---

def test_zscore_entry_is_a_confirmed_crossback(random_walk_df: pd.DataFrame) -> None:
    s = STRATEGIES["zscore_ou"]()
    out = s.generate_signals(random_walk_df, CTX)
    z = zscore(out["close"], s.window)
    ent = out["entry"] == 1
    # Entries only at the cross back up: prior z below -entry_z, current z at/above it.
    assert ((z.shift(1) < -s.entry_z) & (z >= -s.entry_z))[ent].all()
    # And never while price is still stretched below (the old knife-catching level entry).
    assert not ent[z < -s.entry_z].any()


def test_bb_rsi_entry_requires_crossback_above_lower_band(random_walk_df: pd.DataFrame) -> None:
    s = STRATEGIES["bb_rsi_combo"]()
    out = s.generate_signals(random_walk_df, CTX)
    ent = out["entry"] == 1
    assert (out.loc[ent, "close"] >= out.loc[ent, "bb_lower"]).all()


# --- #6 dual_ma hysteresis ---

def test_dual_ma_has_a_hysteresis_band() -> None:
    assert STRATEGIES["dual_ma"]().band == 0.01
    assert DualMa(band=0.05).band == 0.05


def test_dual_ma_entry_respects_the_band(random_walk_df: pd.DataFrame) -> None:
    s = STRATEGIES["dual_ma"]()
    out = s.generate_signals(random_walk_df, CTX)
    fast, slow = sma(out["close"], s.fast), sma(out["close"], s.slow)
    ent = out["entry"] == 1
    assert (fast[ent] > slow[ent] * (1.0 + s.band)).all()


# --- #8 seasonality trend filter ---

def test_seasonality_entries_are_gated_by_the_uptrend(random_walk_df: pd.DataFrame) -> None:
    trend = sma(random_walk_df["close"], TREND_WINDOW)
    for name in ("day_of_week", "turn_of_month", "month_of_year"):
        out = STRATEGIES[name]().generate_signals(random_walk_df, CTX)
        ent = out["entry"] == 1
        assert (out.loc[ent, "close"] > trend[ent]).all(), name


# --- #5 graded conviction ---

def test_bollinger_and_rsi_emit_graded_conviction(random_walk_df: pd.DataFrame) -> None:
    for name in ("bollinger", "rsi_meanrevert"):
        out = STRATEGIES[name]().generate_signals(random_walk_df, CTX)
        ss = out["signal_strength"].dropna()
        assert (ss >= 0.0).all() and (ss <= 1.0).all(), name
        assert ss.nunique() > 1, f"{name} conviction is constant, not graded"
