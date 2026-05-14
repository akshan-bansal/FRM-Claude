from __future__ import annotations

import pandas as pd
import pytest

from trading_live_claude.signals.indicators import atr, bollinger, donchian, ema, macd, rsi, sma


def test_sma_window_alignment(random_walk_df: pd.DataFrame) -> None:
    s = sma(random_walk_df["close"], window=20)
    assert s.isna().sum() == 19
    assert not s.iloc[20:].isna().any()


def test_ema_no_lookahead(random_walk_df: pd.DataFrame) -> None:
    close = random_walk_df["close"]
    full = ema(close, span=20)
    truncated = ema(close.iloc[:-50], span=20)
    pd.testing.assert_series_equal(
        full.iloc[: len(truncated)], truncated, check_names=False
    )


def test_rsi_range(random_walk_df: pd.DataFrame) -> None:
    r = rsi(random_walk_df["close"], window=14).dropna()
    assert (r >= 0).all() and (r <= 100).all()


def test_macd_columns(random_walk_df: pd.DataFrame) -> None:
    m = macd(random_walk_df["close"])
    assert set(m.columns) == {"macd", "signal", "hist"}


def test_atr_positive(random_walk_df: pd.DataFrame) -> None:
    a = atr(random_walk_df, window=14).dropna()
    assert (a > 0).all()


def test_bollinger_band_order(random_walk_df: pd.DataFrame) -> None:
    b = bollinger(random_walk_df["close"]).dropna()
    assert (b["bb_upper"] >= b["bb_mid"]).all()
    assert (b["bb_mid"] >= b["bb_lower"]).all()


def test_donchian_high_ge_low(random_walk_df: pd.DataFrame) -> None:
    d = donchian(random_walk_df, window=20).dropna()
    assert (d["don_upper"] >= d["don_lower"]).all()


@pytest.mark.parametrize("window", [5, 14, 30])
def test_no_negative_shift(window: int) -> None:
    """Guard against future-leaking implementations: shifting input by -1 must change output."""
    close = pd.Series(range(1, 200), dtype=float)
    base = ema(close, span=window)
    leaked = ema(close.shift(-1), span=window)
    assert not base.equals(leaked)
