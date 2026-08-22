from __future__ import annotations

import pandas as pd
import pytest

from trading_live_claude.strategies import STRATEGIES
from trading_live_claude.strategies.base import StrategyContext

# The strategy families added in Phase 1.
NEW_STRATEGIES = [
    "rsi2_connors", "zscore_ou", "bb_rsi_combo",
    "ts_momentum", "dual_ma", "high_52w_breakout",
    "atr_channel", "bbwidth_squeeze", "vol_target",
    "turn_of_month", "day_of_week", "month_of_year",
]


@pytest.mark.parametrize("name", NEW_STRATEGIES)
def test_registered(name: str) -> None:
    assert name in STRATEGIES


@pytest.mark.parametrize("name", NEW_STRATEGIES)
def test_valid_signal_columns(name: str, random_walk_df: pd.DataFrame) -> None:
    out = STRATEGIES[name]().generate_signals(random_walk_df, StrategyContext(symbol="T"))
    assert {"entry", "exit", "atr", "signal_strength"}.issubset(out.columns)
    assert out["entry"].dropna().isin([0, 1]).all()
    assert out["exit"].dropna().isin([0, 1]).all()
    strength = out["signal_strength"].dropna()
    assert (strength >= 0.0).all() and (strength <= 1.0).all()


@pytest.mark.parametrize("name", NEW_STRATEGIES)
def test_no_lookahead_truncation_invariant(name: str, random_walk_df: pd.DataFrame) -> None:
    """A signal at bar t must not depend on any bar after t.

    Compute signals on the full series and on a series truncated at bar 400; the
    entry/exit values on a slice safely inside both must be byte-identical. A
    strategy that peeked into the future would diverge.
    """
    ctx = StrategyContext(symbol="T")
    strat = STRATEGIES[name]
    full = strat().generate_signals(random_walk_df, ctx)
    trunc = strat().generate_signals(random_walk_df.iloc[:400].copy(), ctx)

    sl = slice(300, 400)
    pd.testing.assert_series_equal(
        full["entry"].iloc[sl].reset_index(drop=True),
        trunc["entry"].iloc[sl].reset_index(drop=True),
        check_names=False,
    )
    pd.testing.assert_series_equal(
        full["exit"].iloc[sl].reset_index(drop=True),
        trunc["exit"].iloc[sl].reset_index(drop=True),
        check_names=False,
    )


def test_families_span_all_four_categories() -> None:
    # Sanity: we actually added mean-reversion, momentum, volatility, seasonality.
    for anchor in ("zscore_ou", "ts_momentum", "atr_channel", "turn_of_month"):
        assert anchor in STRATEGIES
