"""Vectorized technical indicators. Pure pandas/numpy, no third-party TA libs.

Every indicator returns Series aligned to the input DataFrame's index. None of
them peek at the future (no centered windows, no future-shifted closes).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window=window, min_periods=window).mean()


def ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).rename("rsi")


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    line = ema_fast - ema_slow
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean().rename("atr")


def bollinger(close: pd.Series, window: int = 20, n_std: float = 2.0) -> pd.DataFrame:
    mid = sma(close, window)
    std = close.rolling(window=window, min_periods=window).std(ddof=0)
    return pd.DataFrame({"bb_mid": mid, "bb_upper": mid + n_std * std, "bb_lower": mid - n_std * std})


def donchian(df: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    high = df["high"].rolling(window=window, min_periods=window).max()
    low = df["low"].rolling(window=window, min_periods=window).min()
    return pd.DataFrame({"don_upper": high, "don_lower": low, "don_mid": (high + low) / 2.0})


def zscore(s: pd.Series, window: int = 20) -> pd.Series:
    mu = s.rolling(window=window, min_periods=window).mean()
    sigma = s.rolling(window=window, min_periods=window).std(ddof=0)
    return ((s - mu) / sigma.replace(0, np.nan)).rename("zscore")
