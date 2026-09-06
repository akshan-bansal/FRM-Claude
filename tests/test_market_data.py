"""MarketData cache-boundary + interval-standardization tests.

Regression coverage for two live-path landmines fixed 2026-09-05:

* Prior to the fix, ``end`` came from ``datetime.now(UTC)`` and became part of the cache
  key, so two calls seconds apart hashed differently and the cache never hit — every
  ``LiveMonitor.step()`` symbol was a fresh live-broker fetch. The floor-to-interval
  helper makes the cache key stable within a bar.
* Prior to the fix, ``MarketData`` emitted Questrade's ``HalfHour`` word for 30-minute
  requests, which every non-QT broker silently rejected and downgraded to daily bars.
  The lexicon is now IB/Kraken's (``ThirtyMinutes``) with a Questrade-side alias.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from trading_live_claude.brokers.models import Candle
from trading_live_claude.data.cache import CandleCache
from trading_live_claude.data.market import (
    _INTERVAL_MAP,
    MarketData,
    _floor_to_interval,
)


def _mk_broker(candles: list[Candle]) -> MagicMock:
    broker = MagicMock()
    broker.candles = MagicMock(return_value=candles)
    return broker


def test_floor_to_interval_daily_boundary_is_stable_within_day() -> None:
    morning = datetime(2026, 9, 5, 9, 30, tzinfo=UTC)
    evening = datetime(2026, 9, 5, 22, 15, tzinfo=UTC)
    assert _floor_to_interval(morning, "1d") == _floor_to_interval(evening, "1d")
    assert _floor_to_interval(morning, "1d") == datetime(2026, 9, 5, tzinfo=UTC)


def test_floor_to_interval_thirty_minute_boundary() -> None:
    ts = datetime(2026, 9, 5, 14, 47, 30, tzinfo=UTC)
    floored = _floor_to_interval(ts, "30m")
    assert floored == datetime(2026, 9, 5, 14, 30, tzinfo=UTC)


def test_interval_map_uses_ib_kraken_lexicon() -> None:
    # The 30-minute bar is the specific mismatch that regressed silently. Every other
    # interval already agreed across brokers.
    assert _INTERVAL_MAP["30m"] == "ThirtyMinutes"
    # Every value maps to a word IB and Kraken both recognize
    for wire in _INTERVAL_MAP.values():
        assert wire in {"OneMinute", "FiveMinutes", "FifteenMinutes", "ThirtyMinutes",
                        "OneHour", "FourHours", "OneDay", "OneWeek"}


def test_market_data_cache_hits_on_second_call_within_bar(tmp_path: Path) -> None:
    # Two calls seconds apart used to hash different keys (datetime.now moves). Now
    # both floor to the same bar boundary and the second call reads the cache.
    day1 = datetime(2026, 9, 1, tzinfo=UTC)
    day2 = datetime(2026, 9, 2, tzinfo=UTC)
    one_day = timedelta(days=1)
    candles = [
        Candle(start=day1, end=day1 + one_day, open=100.0, high=101.0,
               low=99.5, close=100.5, volume=1000),
        Candle(start=day2, end=day2 + one_day, open=100.5, high=102.0,
               low=100.0, close=101.5, volume=1200),
    ]
    broker = _mk_broker(candles)
    cache = CandleCache(root=tmp_path)
    md = MarketData(broker, cache=cache)

    md.history("SPY", years=1.0, interval="1d")
    md.history("SPY", years=1.0, interval="1d")
    md.history("SPY", years=1.0, interval="1d")
    # First call fetches; second + third hit the cache.
    assert broker.candles.call_count == 1


def test_market_data_forwards_standardized_interval_to_broker(tmp_path: Path) -> None:
    broker = _mk_broker([])
    md = MarketData(broker, cache=CandleCache(root=tmp_path))
    md.history("SPY", years=0.5, interval="30m")
    # Broker gets the IB/Kraken word, never Questrade's "HalfHour"
    _, _, _, wire = broker.candles.call_args[0]
    assert wire == "ThirtyMinutes"


def test_questrade_broker_translates_thirtyminutes_to_halfhour() -> None:
    # Questrade REST uses its own lexicon; the broker translates the standardized
    # word on its own side so MarketData stays broker-agnostic.
    from trading_live_claude.brokers.questrade import _QT_INTERVAL_ALIAS

    assert _QT_INTERVAL_ALIAS["ThirtyMinutes"] == "HalfHour"
    # Unrecognized words pass through unchanged
    assert _QT_INTERVAL_ALIAS.get("OneDay", "OneDay") == "OneDay"
