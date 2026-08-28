from __future__ import annotations

import pandas as pd
import pytest

from trading_live_claude.strategies import STRATEGIES
from trading_live_claude.strategies.base import StrategyContext

# Pairs strategies need a partner-leg "close_b" column, so they can't run on a single-symbol frame.
_PAIRS = {"pairs", "kalman_pairs"}
# arima_garch refits ARIMA/GARCH per bar; it has its own fast test (test_arima_garch.py) instead
# of running on the 500-bar generic fixture, which would dominate the suite's runtime.
_SLOW = {"arima_garch"}


@pytest.mark.parametrize("name", [n for n in STRATEGIES if n not in _PAIRS | _SLOW])
def test_strategy_produces_signal_columns(name: str, random_walk_df: pd.DataFrame) -> None:
    strat = STRATEGIES[name]()
    out = strat.generate_signals(random_walk_df, StrategyContext(symbol="TEST"))
    assert {"entry", "exit"}.issubset(out.columns)
    assert out["entry"].dropna().isin([0, 1]).all()
    assert out["exit"].dropna().isin([0, 1]).all()
    assert "atr" in out.columns


@pytest.mark.parametrize("name", [n for n in STRATEGIES if n not in _PAIRS])
def test_strategy_required_history_at_least_50(name: str) -> None:
    strat = STRATEGIES[name]()
    assert strat.required_history_bars() >= 50


def test_ema_crossover_fires_on_trend(trending_df: pd.DataFrame) -> None:
    strat = STRATEGIES["ema_crossover"]()
    out = strat.generate_signals(trending_df, StrategyContext(symbol="TREND"))
    # On a clean uptrend the fast EMA should cross above slow at least once.
    assert out["entry"].sum() >= 1


def test_no_negative_shift_in_strategy_module() -> None:
    """Cheap regression test for lookahead bias: no `.shift(-N)` in strategy modules."""
    import importlib
    import inspect

    for name in STRATEGIES:
        cls = STRATEGIES[name]
        src = inspect.getsource(importlib.import_module(cls.__module__))
        assert "shift(-" not in src, f"{name} module contains shift(-N) -- possible lookahead"
