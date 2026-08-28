from __future__ import annotations

import numpy as np
import pandas as pd

from trading_live_claude.strategies import STRATEGIES
from trading_live_claude.strategies.base import StrategyContext


def _frame(n: int = 260, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    drift = np.concatenate([np.full(n // 2, 0.0012), np.full(n - n // 2, -0.0008)])
    close = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.01, n)))
    return pd.DataFrame({"open": close, "high": close * 1.004, "low": close * 0.996,
                         "close": close, "volume": 1e6})


def test_arima_garch_registered() -> None:
    assert "arima_garch" in STRATEGIES


def test_arima_garch_produces_valid_columns() -> None:
    sig = STRATEGIES["arima_garch"](window=100).generate_signals(_frame(), StrategyContext(symbol="X"))
    for col in ("entry", "exit", "atr", "ma_trend", "arima_fc", "garch_vol", "signal_strength"):
        assert col in sig
    assert sig["entry"].isin([0, 1]).all()
    assert sig["exit"].isin([0, 1]).all()
    assert sig["signal_strength"].between(0.0, 1.0).all()


def test_arima_garch_has_no_lookahead() -> None:
    df = _frame()
    full = STRATEGIES["arima_garch"](window=100).generate_signals(df, StrategyContext(symbol="X"))["entry"].to_numpy()
    part = STRATEGIES["arima_garch"](window=100).generate_signals(df.iloc[:200].copy(), StrategyContext(symbol="X"))["entry"].to_numpy()
    assert np.array_equal(full[:180], part[:180])
