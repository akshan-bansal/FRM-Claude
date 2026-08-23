from __future__ import annotations

import numpy as np
import pandas as pd

from trading_live_claude.signals.candlesticks import (
    CANDLESTICK_PATTERNS,
    bullish_engulfing,
    detect_all,
    doji,
    hammer,
    morning_star,
)


def _df(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    o, h, low, c = zip(*rows, strict=True)
    n = len(rows)
    return pd.DataFrame(
        {
            "time": pd.date_range("2022-01-01", periods=n, freq="B", tz="UTC"),
            "open": o, "high": h, "low": low, "close": c, "volume": [1e6] * n,
        }
    )


def test_registry_has_at_least_40_patterns() -> None:
    assert len(CANDLESTICK_PATTERNS) >= 40


def test_doji_fires_on_tiny_body() -> None:
    df = _df([(100.0, 101.0, 99.0, 100.02)])  # body 0.02 vs range 2 → doji
    assert bool(doji(df).iloc[0])


def test_bullish_engulfing_constructed() -> None:
    df = _df([(101.0, 101.2, 98.8, 99.0), (98.5, 101.6, 98.3, 101.5)])
    assert bool(bullish_engulfing(df).iloc[1])
    assert not bool(bullish_engulfing(df).iloc[0])


def test_hammer_needs_downtrend_and_shape() -> None:
    df = _df([
        (110.0, 110.5, 109.5, 110.0), (108.0, 108.5, 107.5, 108.0),
        (106.0, 106.5, 105.5, 106.0), (104.0, 104.5, 103.5, 104.0),
        (102.0, 102.5, 100.0, 102.3),  # small body top, long lower shadow, after a downtrend
    ])
    assert bool(hammer(df).iloc[4])


def test_morning_star_three_bar() -> None:
    df = _df([
        (110.0, 110.2, 103.8, 104.0),  # long bear
        (102.2, 102.6, 101.4, 102.0),  # small body, gaps below
        (102.5, 108.2, 102.3, 108.0),  # bull closing above bar-0 midpoint (107)
    ])
    assert bool(morning_star(df).iloc[2])


def test_detect_all_is_bool_frame() -> None:
    rng = np.random.default_rng(1)
    n = 200
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
    o = c * (1 + rng.normal(0, 0.004, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.004, n)))
    df = _df(list(zip(o, h, low, c, strict=True)))
    d = detect_all(df)
    assert d.shape == (n, len(CANDLESTICK_PATTERNS))
    assert all(d[col].dtype == bool for col in d.columns)


def test_no_lookahead_truncation_invariant() -> None:
    rng = np.random.default_rng(2)
    n = 300
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
    o = c * (1 + rng.normal(0, 0.004, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.004, n)))
    df = _df(list(zip(o, h, low, c, strict=True)))

    full = detect_all(df)
    trunc = detect_all(df.iloc[:250])
    pd.testing.assert_frame_equal(
        full.iloc[200:250].reset_index(drop=True), trunc.iloc[200:250].reset_index(drop=True)
    )
