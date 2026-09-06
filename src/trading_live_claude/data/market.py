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


# Canonical interval lexicon — the IB / Kraken / IB-Web spelling. Every broker except
# Questrade already accepts this vocabulary; QuestradeBroker translates
# ``ThirtyMinutes`` → its REST wire word ``HalfHour`` on its own side. Prior to
# 2026-09-05 this map emitted Questrade's word (``HalfHour``) which every non-QT broker
# silently rejected and fell back to daily bars.
_INTERVAL_MAP: dict[str, str] = {
    "1m": "OneMinute",
    "5m": "FiveMinutes",
    "15m": "FifteenMinutes",
    "30m": "ThirtyMinutes",
    "1h": "OneHour",
    "4h": "FourHours",
    "1d": "OneDay",
    "1w": "OneWeek",
}

# Bar duration in seconds, used to floor ``end`` to a boundary so the cache key stops
# changing on every wall-clock second. Values track the canonical lexicon above.
_INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
    "1w": 7 * 24 * 60 * 60,
}


def _floor_to_interval(ts: datetime, interval: str) -> datetime:
    """Floor ``ts`` to the start of the current bar for this interval.

    This is what fixes the cache-key drift: without it, ``end = datetime.now(UTC)`` moves
    every call so two backtests seconds apart cache under different keys and re-fetch
    the same window. Flooring means every call within the same bar produces the same
    key — cache hits work — and the key rolls forward exactly when a new bar closes
    and there is genuinely new data to fetch.
    """
    seconds = _INTERVAL_SECONDS.get(interval, 24 * 60 * 60)
    # Weekly bars close on Sunday UTC (ISO weekday 7 → 0-indexed 6). For every other
    # interval, integer division of the UTC-epoch by the bar duration produces the
    # start-of-bar timestamp directly.
    if interval == "1w":
        # Floor to Monday 00:00 UTC of the current week — matches ISO week convention
        # and gives one boundary per week regardless of call time.
        day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        return day - timedelta(days=day.weekday())
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp((epoch // seconds) * seconds, tz=UTC)


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
        wire_interval = _INTERVAL_MAP.get(interval, "OneDay")
        end = end or datetime.now(UTC)
        end = _floor_to_interval(end, interval)
        start = end - timedelta(days=int(years * 365))

        if self.cache is not None:
            cached = self.cache.get(symbol, interval, start, end)
            if cached is not None:
                return cached

        rows = self.broker.candles(symbol, start, end, wire_interval)
        df = _candles_to_df(rows)
        if self.cache is not None and not df.empty:
            self.cache.put(symbol, interval, start, end, df)
        return df

    def recent(self, symbol: str, bars: int = 200, interval: str = "1d") -> pd.DataFrame:
        """Get the most recent ``bars`` candles for live-monitor windows."""
        years = max(bars * 1.5 / 252.0, 0.05) if interval == "1d" else 0.05
        df = self.history(symbol, years=years, interval=interval)
        return df.tail(bars).reset_index(drop=True)
