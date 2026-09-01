from __future__ import annotations

import httpx
import pytest
import respx

from trading_live_claude.data.kraken_ohlc import kraken_ohlc

_URL = "https://api.kraken.com/0/public/OHLC"


def _candles() -> dict:
    # [time, open, high, low, close, vwap, volume, count]
    return {"error": [], "result": {
        "XXBTZUSD": [
            [1700000000, "35000.0", "35500.0", "34800.0", "35200.0", "35100.0", "1200.5", 8000],
            [1700086400, "35200.0", "36000.0", "35100.0", "35900.0", "35600.0", "1500.0", 9000],
        ],
        "last": 1700086400,
    }}


@respx.mock
def test_kraken_ohlc_parses_canonical_frame() -> None:
    respx.get(_URL).mock(return_value=httpx.Response(200, json=_candles()))
    df = kraken_ohlc("XBTUSD")
    assert list(df.columns) == ["time", "open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df["close"].iloc[-1] == 35900.0
    assert str(df["time"].dtype).startswith("datetime64") and df["time"].dt.tz is not None
    assert df["open"].dtype == float


@respx.mock
def test_kraken_ohlc_raises_on_api_error() -> None:
    respx.get(_URL).mock(return_value=httpx.Response(200, json={"error": ["EQuery:Unknown asset pair"], "result": {}}))
    with pytest.raises(ValueError, match="Kraken OHLC error"):
        kraken_ohlc("NOTAPAIR")


@respx.mock
def test_kraken_ohlc_raises_on_empty_result() -> None:
    respx.get(_URL).mock(return_value=httpx.Response(200, json={"error": [], "result": {"last": 1}}))
    with pytest.raises(ValueError, match="no candles"):
        kraken_ohlc("XBTUSD")


# --- deep history via paginated /0/public/Trades -----------------------------

from pathlib import Path

from trading_live_claude.data.kraken_ohlc import (
    aggregate_trades_to_daily,
    kraken_ohlc_deep,
    kraken_trades_paginated,
)

_TRADES_URL = "https://api.kraken.com/0/public/Trades"


def _trade(price: float, volume: float, time_s: float, side: str = "b") -> list[object]:
    """[price, volume, time_seconds, side, ordertype, misc] — Kraken's row order."""
    return [str(price), str(volume), time_s, side, "l", ""]


def _trades_page(pair: str, trades: list[list[object]], last_cursor: str) -> dict:
    return {"error": [], "result": {pair: trades, "last": last_cursor}}


@respx.mock
def test_paginated_trades_walk_the_cursor_and_stop_on_empty_page() -> None:
    """Two full pages then an empty one → the walker stops after the empty."""
    pair = "XBTUSD"
    page_a = _trades_page(pair, [
        _trade(30_000.0, 0.5, 1_700_000_000.1),
        _trade(30_050.0, 0.3, 1_700_000_010.2),
    ], last_cursor="1700000010200000000")
    page_b = _trades_page(pair, [
        _trade(30_100.0, 0.7, 1_700_000_020.3),
    ], last_cursor="1700000020300000000")
    page_c = _trades_page(pair, [], last_cursor="1700000020300000000")
    respx.get(_TRADES_URL).mock(side_effect=[
        httpx.Response(200, json=page_a),
        httpx.Response(200, json=page_b),
        httpx.Response(200, json=page_c),
    ])
    df = kraken_trades_paginated(pair, since_ns="0", sleep_s=0.0)
    assert list(df.columns) == ["time", "price", "volume", "side"]
    assert len(df) == 3
    assert df["price"].tolist() == [30_000.0, 30_050.0, 30_100.0]
    # Oldest-first invariant.
    assert df["time"].is_monotonic_increasing


@respx.mock
def test_paginated_trades_stop_when_cursor_does_not_advance() -> None:
    """Server returns the same cursor twice → walker terminates rather than looping forever."""
    pair = "XBTUSD"
    page = _trades_page(pair, [_trade(31_000.0, 0.1, 1_700_000_000.0)],
                        last_cursor="1700000000000000000")
    # Every subsequent call returns the SAME cursor -> the walker must stop.
    respx.get(_TRADES_URL).mock(return_value=httpx.Response(200, json=page))
    df = kraken_trades_paginated(pair, since_ns="1700000000000000000", sleep_s=0.0, max_pages=10)
    # One page consumed, then cursor-equality termination on the second iteration.
    assert len(df) == 1


@respx.mock
def test_paginated_trades_max_pages_is_a_hard_cap() -> None:
    """max_pages bounds the walker even when the server keeps advancing forever."""
    pair = "XBTUSD"
    counter = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        # Cursor always advances so the walker cannot terminate on the equality check.
        return httpx.Response(200, json=_trades_page(
            pair,
            [_trade(31_000.0 + counter["n"], 0.1, 1_700_000_000.0 + counter["n"])],
            last_cursor=str(1_700_000_000_000_000_000 + counter["n"]),
        ))
    respx.get(_TRADES_URL).mock(side_effect=_handler)
    df = kraken_trades_paginated(pair, since_ns="0", sleep_s=0.0, max_pages=3)
    assert counter["n"] == 3
    assert len(df) == 3


def test_aggregate_trades_to_daily_produces_correct_ohlcv() -> None:
    """One day of five trades → open == first, close == last, high/low correct, volume summed."""
    import pandas as pd
    ts = pd.to_datetime("2026-08-31", utc=True)
    trades = pd.DataFrame({
        "time":   [ts + pd.Timedelta(hours=h) for h in (0, 4, 8, 12, 16)],
        "price":  [100.0, 102.0, 98.0, 101.0, 103.0],
        "volume": [1.0, 2.0, 1.5, 0.5, 1.0],
        "side":   list("bsbsb"),
    })
    daily = aggregate_trades_to_daily(trades)
    assert len(daily) == 1
    row = daily.iloc[0]
    assert row["open"] == 100.0
    assert row["close"] == 103.0
    assert row["high"] == 103.0
    assert row["low"] == 98.0
    assert row["volume"] == 6.0


def test_aggregate_days_without_trades_are_omitted_not_zero_filled() -> None:
    """Zero-volume forward-fill would corrupt every strategy reading volume as liquidity."""
    import pandas as pd
    ts0 = pd.to_datetime("2026-08-29", utc=True)
    ts2 = pd.to_datetime("2026-08-31", utc=True)
    trades = pd.DataFrame({
        "time":   [ts0, ts2],
        "price":  [100.0, 105.0],
        "volume": [1.0, 1.0],
        "side":   ["b", "s"],
    })
    daily = aggregate_trades_to_daily(trades)
    # Only two rows even though ts1 (Aug 30) lies between them.
    assert len(daily) == 2
    assert daily["volume"].min() > 0


def test_aggregate_empty_trades_returns_an_empty_frame_with_the_right_columns() -> None:
    import pandas as pd
    daily = aggregate_trades_to_daily(pd.DataFrame(
        columns=["time", "price", "volume", "side"]))
    assert list(daily.columns) == ["time", "open", "high", "low", "close", "volume"]
    assert daily.empty


@respx.mock
def test_kraken_ohlc_deep_writes_cache_when_cache_dir_given(tmp_path: Path) -> None:
    """End-to-end: paginate → aggregate → write parquet under cache_dir."""
    pair = "XBTUSD"
    page_a = _trades_page(pair, [
        _trade(30_000.0, 0.5, 1_700_000_000.1),
        _trade(30_050.0, 0.3, 1_700_086_400.0),      # next day
    ], last_cursor="1700086400000000000")
    page_b = _trades_page(pair, [], last_cursor="1700086400000000000")
    respx.get(_TRADES_URL).mock(side_effect=[
        httpx.Response(200, json=page_a),
        httpx.Response(200, json=page_b),
    ])
    df = kraken_ohlc_deep(pair, since_ns="0", cache_dir=tmp_path, sleep_s=0.0)
    assert len(df) == 2
    assert (tmp_path / f"{pair}_trades.parquet").exists()
    assert (tmp_path / f"{pair}_daily.parquet").exists()


@respx.mock
def test_progress_callback_receives_page_and_trade_counts() -> None:
    """A script can attach a progress printer without touching stdout during tests."""
    pair = "XBTUSD"
    p1 = _trades_page(pair, [_trade(30_000.0, 1.0, 1_700_000_000.0)], last_cursor="1")
    p2 = _trades_page(pair, [_trade(30_100.0, 1.0, 1_700_000_060.0)], last_cursor="2")
    p3 = _trades_page(pair, [], last_cursor="2")
    respx.get(_TRADES_URL).mock(side_effect=[
        httpx.Response(200, json=p1),
        httpx.Response(200, json=p2),
        httpx.Response(200, json=p3),
    ])
    seen: list[tuple[int, int]] = []
    kraken_trades_paginated(pair, since_ns="0", sleep_s=0.0,
                            progress=lambda pg, n: seen.append((pg, n)))
    assert seen == [(1, 1), (2, 2)]           # page 3 was empty and does NOT call progress
