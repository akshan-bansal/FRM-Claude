"""Market-data facade. Thin wrapper around broker + cache that produces
pandas DataFrames in a canonical shape used by every downstream module.

Canonical OHLCV columns: ``[time, open, high, low, close, volume]``.
``time`` is timezone-aware (UTC).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from ..brokers.base import Broker
from ..brokers.models import Candle
from .cache import CandleCache


_QT_INTERVAL_MAP = {
    "1m": "OneMinute",
    "5m": "FiveMinutes",
    "15m": "FifteenMinutes",
    "30m": "HalfHour",
    "1h": "OneHour",
    "1d": "OneDay",
    "1w": "OneWeek",
}


def _candles_to_df(rows: list[Candle]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame([c.model_dump() for c in rows])
    df = df.rename(columns={"start": "time"})
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df[["time", "open", "high", "low", "close", "volume"]].sort_values("time").reset_index(drop=True)


class MarketData:
    def __init__(self, broker: Broker, cache: CandleCache | None = None) -> None:
        self.broker = broker
        self.cache = cache

    def history(
        self,
        symbol: str,
        years: float = 3.0,
        interval: str = "1d",
        end: datetime | None = None,
    ) -> pd.DataFrame:
        qt_interval = _QT_INTERVAL_MAP.get(interval, "OneDay")
        end = end or datetime.now(UTC)
        start = end - timedelta(days=int(years * 365))

        if self.cache is not None:
            cached = self.cache.get(symbol, interval, start, end)
            if cached is not None:
                return cached

        rows = self.broker.candles(symbol, start, end, qt_interval)
        df = _candles_to_df(rows)
        if self.cache is not None and not df.empty:
            self.cache.put(symbol, interval, start, end, df)
        return df

    def recent(self, symbol: str, bars: int = 200, interval: str = "1d") -> pd.DataFrame:
        """Get the most recent ``bars`` candles for live-monitor windows."""
        years = max(bars * 1.5 / 252.0, 0.05) if interval == "1d" else 0.05
        df = self.history(symbol, years=years, interval=interval)
        return df.tail(bars).reset_index(drop=True)
