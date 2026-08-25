from __future__ import annotations

import numpy as np
import pandas as pd

from trading_live_claude.backtest import BacktestEngine
from trading_live_claude.strategies import STRATEGIES
from trading_live_claude.strategies.base import StrategyContext
from trading_live_claude.strategies.examples.ema_crossover import EmaCrossover

CTX = StrategyContext(symbol="T")
SHORTABLE = ("ema_crossover", "macd", "momentum_breakout", "dual_ma")


def _down_then_flat(n: int = 320) -> pd.DataFrame:
    # 60 bars flat, then a sustained decline → a downward cross opens a short.
    close = np.concatenate([np.full(60, 100.0), 100 * np.exp(np.cumsum(np.full(n - 60, -0.01)))])
    return pd.DataFrame({
        "time": pd.date_range("2021-01-01", periods=n, freq="B", tz="UTC"),
        "open": close, "high": close * 1.005, "low": close * 0.995, "close": close, "volume": [1e6] * n,
    })


def test_shortable_strategies_emit_the_short_channel() -> None:
    df = _down_then_flat()
    for name in SHORTABLE:
        out = STRATEGIES[name]().generate_signals(df, CTX)
        assert {"short_entry", "short_exit"}.issubset(out.columns), name
        assert out["short_entry"].dropna().isin([0, 1]).all(), name


def test_allow_short_false_is_long_only() -> None:
    out = EmaCrossover(allow_short=False).generate_signals(_down_then_flat(), CTX)
    assert "short_entry" not in out.columns and "short_exit" not in out.columns


def test_downtrend_actually_opens_a_profitable_short() -> None:
    res = BacktestEngine().run(EmaCrossover(fast=10, slow=30), _down_then_flat(), symbol="T")
    assert (res.positions == -1).any(), "a sustained decline should open a short"
    # Shorting a falling market makes money — the ceiling the long-only design left on the table.
    assert res.metrics.total_return > 0.0


def test_long_only_and_short_positions_differ_in_backtest() -> None:
    df = _down_then_flat()
    short_pos = BacktestEngine().run(EmaCrossover(fast=10, slow=30), df, symbol="T").positions
    long_only = BacktestEngine().run(EmaCrossover(fast=10, slow=30, allow_short=False), df, symbol="T").positions
    assert (short_pos == -1).any()
    assert not (long_only == -1).any()  # long-only never shorts
