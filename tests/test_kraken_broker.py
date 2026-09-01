"""KrakenBroker tests. Covers the public data surface (auth-free) end-to-end, and pins the
live-order gate as a hard refusal until enable_live_orders=True is explicit.

Private endpoints are NOT exercised against a network — those go through the
already-tested :mod:`kraken_auth.private_post` helper and add nothing new.
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from trading_live_claude.brokers.base import OrderRejected
from trading_live_claude.brokers.kraken import (
    KrakenBroker,
    to_kraken_pair,
)
from trading_live_claude.brokers.models import Order, OrderAction, OrderType

_TICKER = "https://api.kraken.com/0/public/Ticker"
_OHLC = "https://api.kraken.com/0/public/OHLC"


def _ticker_response(pairs: dict[str, tuple[float, float, float]]) -> dict:
    """Kraken responds keyed by its canonical pair code (XXBTZUSD, not XBTUSD).

    Each pair is (bid, ask, last).
    """
    result: dict[str, dict[str, list[str]]] = {}
    for key, (bid, ask, last) in pairs.items():
        result[key] = {
            "a": [str(ask), "1", "1.000"],       # ask, whole-lot volume, lot volume
            "b": [str(bid), "1", "1.000"],       # bid, ...
            "c": [str(last), "0.05"],            # last trade price, volume
        }
    return {"error": [], "result": result}


@respx.mock
def test_quote_translates_routed_symbol_to_wire_pair_and_parses_response() -> None:
    """BTC/USD → XBTUSD on the wire; the response is keyed XXBTZUSD; the broker resolves both."""
    respx.get(_TICKER).mock(return_value=httpx.Response(200,
        json=_ticker_response({"XXBTZUSD": (30_000.0, 30_010.0, 30_005.0)})))
    with KrakenBroker() as b:
        q = b.quote("BTC/USD")
    assert q.symbol == "BTC/USD"
    assert q.bidPrice == 30_000.0
    assert q.askPrice == 30_010.0
    assert q.lastTradePrice == 30_005.0
    assert q.mid == 30_005.0


@respx.mock
def test_quotes_batches_multiple_symbols_into_one_request() -> None:
    """One HTTP round-trip regardless of how many symbols the caller asks for."""
    route = respx.get(_TICKER).mock(return_value=httpx.Response(200,
        json=_ticker_response({
            "XXBTZUSD": (30_000.0, 30_010.0, 30_005.0),
            "XETHZUSD": (2_000.0, 2_001.5, 2_000.75),
        })))
    with KrakenBroker() as b:
        qs = b.quotes(["BTC/USD", "ETH/USD"])
    assert len(qs) == 2
    assert route.call_count == 1
    assert {q.symbol for q in qs} == {"BTC/USD", "ETH/USD"}


@respx.mock
def test_quote_of_unknown_pair_returns_empty_rather_than_raising() -> None:
    """Missing datum is a quote with no prices — PaperBroker's reject path handles it upstream."""
    respx.get(_TICKER).mock(return_value=httpx.Response(200, json={"error": [], "result": {}}))
    with KrakenBroker() as b:
        q = b.quote("NOSUCH/USD")
    assert q.bidPrice is None and q.askPrice is None and q.lastTradePrice is None
    assert q.mid is None


@respx.mock
def test_candles_translates_pair_and_windows_the_result() -> None:
    """Public OHLC returns [time, open, high, low, close, vwap, volume, count] rows."""
    respx.get(_OHLC).mock(return_value=httpx.Response(200, json={"error": [], "result": {
        "XXBTZUSD": [
            [1_700_000_000, "30000", "30100", "29900", "30050", "30020", "1.5", 100],
            [1_700_086_400, "30050", "30200", "30000", "30150", "30100", "2.0", 150],
        ],
        "last": 1_700_086_400,
    }}))
    with KrakenBroker() as b:
        got = b.candles("BTC/USD",
                         start=datetime.fromtimestamp(1_700_000_000, tz=UTC),
                         end=datetime.fromtimestamp(1_700_100_000, tz=UTC),
                         interval="OneDay")
    assert len(got) == 2
    assert got[0].open == 30000.0
    assert got[1].close == 30150.0


@respx.mock
def test_candles_returns_empty_when_the_endpoint_has_no_series() -> None:
    respx.get(_OHLC).mock(return_value=httpx.Response(200,
                                                      json={"error": [], "result": {"last": 1}}))
    with KrakenBroker() as b:
        got = b.candles("BTC/USD",
                         start=datetime.now(UTC), end=datetime.now(UTC), interval="OneDay")
    assert got == []


def test_pair_translation_covers_the_sleeve_and_falls_through_unknowns() -> None:
    assert to_kraken_pair("BTC/USD") == "XBTUSD"
    assert to_kraken_pair("ETH/USD") == "ETHUSD"
    assert to_kraken_pair("PAXG/USD") == "PAXGUSD"
    # Anything not in the small mapping is passed through unchanged.
    assert to_kraken_pair("DOGE/USD") == "DOGE/USD"


def test_accounts_synthesizes_a_stable_account_row() -> None:
    """One row, primary, so the router always sees a stable account id."""
    with KrakenBroker() as b:
        accs = b.accounts()
    assert len(accs) == 1 and accs[0].isPrimary and accs[0].number == "KRAKEN"


def test_positions_are_empty_without_credentials() -> None:
    """No API key → no Balance call → no positions. Same for equity."""
    with KrakenBroker() as b:
        assert b.positions("KRAKEN") == []
        assert b.equity("KRAKEN", "USD") == 0.0


def test_place_order_refuses_loudly_by_default() -> None:
    """The go-live gate: never send live unless enable_live_orders=True at construction."""
    order = Order(symbol="BTC/USD", symbolId=0, action=OrderAction.BUY,
                  orderType=OrderType.MARKET, totalQuantity=0.001)
    with KrakenBroker() as b, pytest.raises(OrderRejected, match="live orders disabled"):
        b.place_order(order)


def test_place_order_still_refuses_without_credentials_even_when_enabled() -> None:
    """enable_live_orders=True is not enough on its own — missing creds is a separate refuse."""
    order = Order(symbol="BTC/USD", symbolId=0, action=OrderAction.BUY,
                  orderType=OrderType.MARKET, totalQuantity=0.001)
    with KrakenBroker(enable_live_orders=True) as b, pytest.raises(OrderRejected,
                                                                    match="credentials"):
        b.place_order(order)


def test_cancel_order_is_also_gated_off_by_default() -> None:
    """Cancels are only meaningful when there is something to cancel; the gate applies to both."""
    from trading_live_claude.brokers.base import BrokerError
    with KrakenBroker() as b, pytest.raises(BrokerError, match="disabled"):
        b.cancel_order("KRAKEN", 1)
