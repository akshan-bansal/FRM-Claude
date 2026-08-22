from __future__ import annotations

import pandas as pd
import pytest

from trading_live_claude.analysis.classification import confusion
from trading_live_claude.analysis.labeling import label_events
from trading_live_claude.strategies import STRATEGIES, CompositeStrategy
from trading_live_claude.strategies.base import StrategyContext
from trading_live_claude.strategies.composite import _default_members


def test_composite_emits_strength_and_agreement(random_walk_df: pd.DataFrame) -> None:
    out = CompositeStrategy().generate_signals(random_walk_df, StrategyContext(symbol="T"))
    assert {"entry", "exit", "signal_strength", "n_agree", "atr"}.issubset(out.columns)
    assert out["entry"].dropna().isin([0, 1]).all()
    strength = out["signal_strength"].dropna()
    assert (strength >= 0.0).all() and (strength <= 1.0).all()
    # Agreement fraction must equal n_agree / n_members.
    n_members = len(_default_members())
    assert (out["signal_strength"] * n_members).round().astype(int).equals(out["n_agree"])


def test_composite_recall_dominates_every_member(random_walk_df: pd.DataFrame) -> None:
    """The whole point of the recall stage: OR-union catches >= what any member does."""
    ctx = StrategyContext(symbol="T")
    labels = label_events(random_walk_df, horizon=10, up_threshold=0.02)

    members = _default_members()
    member_recalls = [
        confusion(m.generate_signals(random_walk_df, ctx)["entry"], labels).recall
        for m in members
    ]
    composite = CompositeStrategy(members).generate_signals(random_walk_df, ctx)
    composite_recall = confusion(composite["entry"], labels).recall

    assert composite_recall >= max(member_recalls) - 1e-9


def test_composite_fires_at_least_as_often_as_best_member(random_walk_df: pd.DataFrame) -> None:
    ctx = StrategyContext(symbol="T")
    members = _default_members()
    member_entries = [
        int(m.generate_signals(random_walk_df, ctx)["entry"].fillna(0).sum()) for m in members
    ]
    composite_entries = int(
        CompositeStrategy(members).generate_signals(random_walk_df, ctx)["entry"].sum()
    )
    assert composite_entries >= max(member_entries)


def test_empty_members_rejected() -> None:
    with pytest.raises(ValueError):
        CompositeStrategy(members=[])


def test_composite_registered_and_zero_arg() -> None:
    strat = STRATEGIES["composite"]()
    assert strat.required_history_bars() >= 50
