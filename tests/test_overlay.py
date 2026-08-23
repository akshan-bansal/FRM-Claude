from __future__ import annotations

import pandas as pd
import pytest

from trading_live_claude.analysis.classification import confusion
from trading_live_claude.analysis.labeling import label_events
from trading_live_claude.scoring.selection import family_of
from trading_live_claude.strategies import STRATEGIES
from trading_live_claude.strategies.base import StrategyContext
from trading_live_claude.strategies.overlay import (
    CONFIRM_STRATEGIES,
    CONFIRMED_BASES,
    REVERSAL_CONFIRM,
    ConfirmOverlay,
)

CTX = StrategyContext(symbol="T")


def test_confirm_strategies_are_the_two_keepers() -> None:
    # Only the two mean-reversion bases that measurably gained precision are wired.
    assert set(CONFIRM_STRATEGIES) == {"confirm_bollinger", "confirm_rsi_meanrevert"}
    for name in CONFIRM_STRATEGIES:
        assert name in STRATEGIES
        assert family_of(name) == "mean_reversion"


def test_all_confirm_strategies_zero_arg_constructible() -> None:
    for name in CONFIRM_STRATEGIES:
        strat = STRATEGIES[name]()
        assert strat.required_history_bars() >= 1


def test_gating_is_a_subset_of_base_entries(random_walk_df: pd.DataFrame) -> None:
    """The overlay can only remove entries, never add them (recall <= base)."""
    for base_name, factory in CONFIRMED_BASES.items():
        base = factory()
        base_entry = base.generate_signals(random_walk_df, CTX)["entry"].fillna(0).astype(int)
        gated = ConfirmOverlay(base).generate_signals(random_walk_df, CTX)["entry"].astype(int)
        assert gated.sum() <= base_entry.sum(), base_name
        # Every gated entry must also be a base entry (pure filtering).
        assert bool(((gated == 1) <= (base_entry == 1)).all()), base_name


def test_gating_never_raises_recall(random_walk_df: pd.DataFrame) -> None:
    labels = label_events(random_walk_df, horizon=10, up_threshold=0.02)
    base = CONFIRMED_BASES["bollinger"]()
    base_recall = confusion(base.generate_signals(random_walk_df, CTX)["entry"], labels).recall
    gated_recall = confusion(
        ConfirmOverlay(base).generate_signals(random_walk_df, CTX)["entry"], labels
    ).recall
    assert gated_recall <= base_recall + 1e-9


def test_confirmed_column_matches_entry(random_walk_df: pd.DataFrame) -> None:
    out = STRATEGIES["confirm_bollinger"]().generate_signals(random_walk_df, CTX)
    assert {"entry", "exit", "confirmed"}.issubset(out.columns)
    assert out["entry"].dropna().isin([0, 1]).all()
    assert out["confirmed"].equals(out["entry"])


def test_no_lookahead_truncation_invariant(random_walk_df: pd.DataFrame) -> None:
    """Signals on shared bars are identical whether or not future bars exist."""
    strat = STRATEGIES["confirm_bollinger"]()
    full = strat.generate_signals(random_walk_df, CTX)["entry"].reset_index(drop=True)
    trunc = strat.generate_signals(random_walk_df.iloc[:200], CTX)["entry"].reset_index(drop=True)
    # Compare an interior slice clear of either warm-up edge.
    pd.testing.assert_series_equal(full.iloc[120:200], trunc.iloc[120:200], check_names=False)


def test_rejects_unknown_pattern_and_bad_lookback() -> None:
    base = CONFIRMED_BASES["bollinger"]()
    with pytest.raises(ValueError):
        ConfirmOverlay(base, patterns=("not_a_pattern",))
    with pytest.raises(ValueError):
        ConfirmOverlay(base, lookback=0)


def test_reversal_pack_is_all_known_bullish_patterns() -> None:
    from trading_live_claude.signals.candlesticks import BULLISH_PATTERNS

    assert set(REVERSAL_CONFIRM) <= set(BULLISH_PATTERNS)
