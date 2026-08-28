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
