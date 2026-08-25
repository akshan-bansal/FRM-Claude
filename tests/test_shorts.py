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


def test_short_channel_is_opt_in() -> None:
    df = _down_then_flat()
    for name in SHORTABLE:
        # Default is long-only (the test showed shorts hurt on a bull-market equity basket).
        default_out = STRATEGIES[name]().generate_signals(df, CTX)
        assert "short_entry" not in default_out.columns, name
        # Opting in emits the short channel.
        opted = STRATEGIES[name](allow_short=True).generate_signals(df, CTX)
        assert {"short_entry", "short_exit"}.issubset(opted.columns), name
        assert opted["short_entry"].dropna().isin([0, 1]).all(), name


def test_downtrend_opens_a_profitable_short_when_opted_in() -> None:
    res = BacktestEngine().run(EmaCrossover(fast=10, slow=30, allow_short=True), _down_then_flat(), symbol="T")
    assert (res.positions == -1).any(), "a sustained decline should open a short"
    assert res.metrics.total_return > 0.0


def test_long_only_default_never_shorts() -> None:
    df = _down_then_flat()
    short_pos = BacktestEngine().run(EmaCrossover(fast=10, slow=30, allow_short=True), df, symbol="T").positions
    long_only = BacktestEngine().run(EmaCrossover(fast=10, slow=30), df, symbol="T").positions
    assert (short_pos == -1).any()
    assert not (long_only == -1).any()  # default (opt-in off) never shorts
