from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.strategies import STRATEGIES
from trading_live_claude.strategies.base import StrategyContext


def _pair_frame(seed: int = 7, n: int = 600) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    x = np.cumsum(rng.normal(0, 1, n)) + 100.0
    spread = np.zeros(n)
    for t in range(1, n):
        spread[t] = 0.9 * spread[t - 1] + rng.normal(0, 0.5)
    y = 3.0 + 1.8 * x + spread
    return pd.DataFrame({"open": y, "high": y, "low": y, "close": y, "close_b": x, "volume": 1e6})


def test_kalman_pairs_registered() -> None:
    assert "kalman_pairs" in STRATEGIES


def test_kalman_pairs_trades_a_cointegrated_pair() -> None:
    sig = STRATEGIES["kalman_pairs"]().generate_signals(_pair_frame(), StrategyContext(symbol="A"))
    assert sig["entry"].sum() > 0
    assert sig["exit"].sum() > 0
    for col in ("hedge_ratio", "spread", "spread_z", "signal_strength"):
        assert col in sig
    assert sig["signal_strength"].between(0.0, 1.0).all()


def test_kalman_pairs_has_no_lookahead() -> None:
    """Truncating the series must not change earlier entry decisions."""
    df = _pair_frame()
    full = STRATEGIES["kalman_pairs"]().generate_signals(df, StrategyContext(symbol="A"))["entry"].to_numpy()
    part = STRATEGIES["kalman_pairs"]().generate_signals(df.iloc[:400].copy(), StrategyContext(symbol="A"))["entry"].to_numpy()
    assert np.array_equal(full[:380], part[:380])


def test_kalman_pairs_requires_partner_leg() -> None:
    df = _pair_frame().drop(columns=["close_b"])
    with pytest.raises(ValueError, match="close_b"):
        STRATEGIES["kalman_pairs"]().generate_signals(df, StrategyContext(symbol="A"))
