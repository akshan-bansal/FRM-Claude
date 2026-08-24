"""Equity-valuation features from market price + book value (second-order statistics).

Turns raw price and book value per share into the valuation series the strategies
mean-revert on: **price-to-book** (P/B), **book-to-market** (B/M), and their trailing
rolling z-scores. These are the "second-order credit/valuation statistics on market
price and book values" — a stock's cheapness relative to its own history, rather than
its price level.

Everything here is a pure, **no-lookahead** function: rolling statistics use only the
trailing window ending at the current bar, never future bars, so the features run and
test on synthetic fundamentals with no broker. Book values arrive as a per-bar series
(a snapshot broadcast, or a slow quarterly step) — the data layer will attach a ``bvps``
column later; until then a constant collapses P/B to the price level, keeping every
consumer runnable on plain OHLCV.
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

# With no book value attached, P/B collapses to the price level (bvps = 1).
DEFAULT_BVPS: float = 1.0

# Valuation metrics a strategy can be run on.
VALUATION_METRICS: tuple[str, ...] = ("price_to_book", "book_to_market")

type BvpsSource = float | pd.Series | Callable[[pd.DataFrame], pd.Series] | None


def resolve_bvps(df: pd.DataFrame, bvps: BvpsSource = None, *, default: float = DEFAULT_BVPS) -> pd.Series:
    """Coerce a book-value-per-share source into a positive Series aligned to ``df``.

    Accepts a ``df['bvps']`` column (when present and ``bvps`` is ``None``), a scalar, a
    pre-built Series, or a ``callable(df) -> Series``. Zeros/gaps are forward/back-filled
    so a downstream ratio never divides by zero.
    """
    if bvps is None:
        if "bvps" in df.columns:
            s = df["bvps"].astype(float)
        else:
            s = pd.Series(default, index=df.index, dtype=float)
    elif callable(bvps):
        s = pd.Series(bvps(df), index=df.index, dtype=float)
    elif isinstance(bvps, pd.Series):
        s = bvps.reindex(df.index).astype(float)
    else:
        s = pd.Series(float(bvps), index=df.index, dtype=float)
    return s.replace(0.0, np.nan).ffill().bfill()


def price_to_book(close: pd.Series, bvps: pd.Series) -> pd.Series:
    """P/B = market price / book value per share (low = cheap)."""
    return (close.astype(float) / bvps).rename("pb")


def book_to_market(close: pd.Series, bvps: pd.Series) -> pd.Series:
    """B/M = book value per share / market price (high = cheap)."""
    return (bvps / close.astype(float)).rename("bm")


def rolling_zscore(s: pd.Series, window: int = 63, min_periods: int = 20) -> pd.Series:
    """Trailing z-score of ``s`` — how many sigma the current value sits from its own
    recent mean. Uses only bars up to and including the current one (no lookahead)."""
    mean = s.rolling(window, min_periods=min_periods).mean()
    std = s.rolling(window, min_periods=min_periods).std(ddof=0).replace(0.0, np.nan)
    name = f"{s.name}_z" if s.name else "z"
    return ((s - mean) / std).rename(name)


def valuation_series(df: pd.DataFrame, metric: str = "price_to_book", bvps: BvpsSource = None) -> pd.Series:
    """The chosen valuation ratio as a Series (the series a strategy mean-reverts on)."""
    if metric not in VALUATION_METRICS:
        raise ValueError(f"Unknown valuation metric {metric!r}; choose from {VALUATION_METRICS}")
    b = resolve_bvps(df, bvps)
    if metric == "price_to_book":
        return price_to_book(df["close"], b)
    return book_to_market(df["close"], b)


def valuation_features(df: pd.DataFrame, bvps: BvpsSource = None, *, z_window: int = 63) -> pd.DataFrame:
    """The full valuation feature frame: bvps, P/B, B/M, and their trailing z-scores."""
    b = resolve_bvps(df, bvps)
    pb = price_to_book(df["close"], b)
    bm = book_to_market(df["close"], b)
    return pd.DataFrame(
        {"bvps": b, "pb": pb, "bm": bm, "pb_z": rolling_zscore(pb, z_window), "bm_z": rolling_zscore(bm, z_window)},
        index=df.index,
    )
