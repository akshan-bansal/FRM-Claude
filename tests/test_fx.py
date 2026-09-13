from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading_live_claude.brokers.base import StaleQuote
from trading_live_claude.brokers.fresh import FreshQuoteBroker
from trading_live_claude.brokers.fx import CurrencyNormalizingBroker, FxRates, FxUnavailable
from trading_live_claude.brokers.models import Candle, Order, OrderAction, OrderType, Quote

T0 = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
MIDS = {"USDCAD": 1.37, "GBPUSD": 1.30, "USDJPY": 150.0, "USDHKD": 7.80, "AUDUSD": 0.66}


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, s: float) -> None:
        self.now += timedelta(seconds=s)


class _Pairs:
    def __init__(self, mids: dict[str, float | None]) -> None:
        self.mids = dict(mids)
        self.calls: list[str] = []

    def __call__(self, pair: str) -> float | None:
        self.calls.append(pair)
        return self.mids.get(pair)


def _rates(pairs: _Pairs | None = None, clock: _Clock | None = None, **kw) -> FxRates:
    return FxRates(pairs or _Pairs(MIDS), "CAD", clock=clock or _Clock(), **kw)


@pytest.mark.parametrize(("ccy", "expected"), [
    ("CAD", 1.0),
    ("USD", 1.37),
    ("GBP", 1.30 * 1.37),
    ("JPY", 1.37 / 150.0),
    ("HKD", 1.37 / 7.80),
    ("AUD", 0.66 * 1.37),
])
def test_triangulates_through_usd_into_cad(ccy: str, expected: float) -> None:
    assert _rates().rate(ccy) == pytest.approx(expected)


def test_usd_numeraire_skips_the_cad_leg() -> None:
    rates = FxRates(_Pairs(MIDS), "USD", clock=_Clock())
    assert rates.rate("USD") == 1.0
    assert rates.rate("CAD") == pytest.approx(1 / 1.37)


def test_rates_are_cached_for_ttl_then_refreshed() -> None:
    pairs, clock = _Pairs(MIDS), _Clock()
    rates = _rates(pairs, clock, ttl_s=60)
    rates.rate("USD")
    rates.rate("USD")
    assert pairs.calls == ["USDCAD"]
    clock.advance(61)
    pairs.mids["USDCAD"] = 1.40
    assert rates.rate("USD") == pytest.approx(1.40)
    assert pairs.calls == ["USDCAD", "USDCAD"]


def test_failed_fetch_falls_back_to_cached_rate_until_max_age() -> None:
    pairs, clock = _Pairs(MIDS), _Clock()
    rates = _rates(pairs, clock, ttl_s=60, max_age_s=900)
    rates.rate("USD")
    pairs.mids["USDCAD"] = None
    clock.advance(600)
    assert rates.rate("USD") == pytest.approx(1.37)
    clock.advance(400)
    with pytest.raises(FxUnavailable, match="USDCAD"):
        rates.rate("USD")


def test_fetch_exception_is_treated_as_missing_rate() -> None:
    def boom(pair: str) -> float:
        raise ConnectionError("TWS down")

    with pytest.raises(FxUnavailable):
        FxRates(boom, "CAD", clock=_Clock()).rate("USD")


class _Feed:
    name = "ib"
    venue = "ib"

    def __init__(self) -> None:
        self.orders: list[Order] = []

    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, symbolId=1, bidPrice=100.0, askPrice=101.0,
                     lastTradePrice=100.5, bidSize=10)

    def quotes(self, symbols: list[str]) -> list[Quote]:
        return [self.quote(s) for s in symbols]

    def candles(self, symbol, start, end, interval="OneDay") -> list[Candle]:
        return [Candle(start=T0, end=T0, open=10, high=12, low=9, close=11, volume=500, VWAP=10.5)]

    def place_order(self, order: Order) -> Order:
        self.orders.append(order)
        return order

    def accounts(self): return []
    def positions(self, _): return []
    def equity(self, *a, **k): return 0.0
    def cancel_order(self, *a, **k): pass


def test_quotes_convert_prices_but_not_sizes() -> None:
    broker = CurrencyNormalizingBroker(_Feed(), _rates())
    q = broker.quote("AAPL")
    assert (q.bidPrice, q.askPrice, q.lastTradePrice) == pytest.approx((137.0, 138.37, 137.685))
    assert q.bidSize == 10
    assert broker.quote("XIC.TO").bidPrice == 100.0


def test_candles_convert_ohlc_and_vwap_but_keep_returns() -> None:
    (bar,) = CurrencyNormalizingBroker(_Feed(), _rates()).candles("VOD.L", T0, T0)
    f = 1.30 * 1.37
    assert (bar.open, bar.high, bar.low, bar.close, bar.VWAP) == pytest.approx(
        (10 * f, 12 * f, 9 * f, 11 * f, 10.5 * f))
    assert bar.volume == 500
    assert bar.close / bar.open == pytest.approx(11 / 10)


def test_missing_fx_reads_as_a_stale_quote() -> None:
    broker = CurrencyNormalizingBroker(_Feed(), _rates(_Pairs({})))
    with pytest.raises(StaleQuote, match="USDJPY"):
        broker.quote("7203.T")


def test_order_prices_convert_back_to_native_currency() -> None:
    feed = _Feed()
    broker = CurrencyNormalizingBroker(feed, _rates())
    broker.place_order(Order(symbol="AAPL", symbolId=1, action=OrderAction.BUY,
                             orderType=OrderType.LIMIT, totalQuantity=5, limitPrice=137.0))
    assert feed.orders[0].limitPrice == pytest.approx(100.0)
    assert feed.orders[0].stopPrice is None


def test_frozen_native_feed_stays_frozen_when_fx_moves() -> None:
    """The stale guard must sit inside the converter, or FX drift would mask a dead feed."""
    clock = _Clock()
    pairs = _Pairs(MIDS)
    broker = CurrencyNormalizingBroker(
        FreshQuoteBroker(_Feed(), max_frozen_s=900, clock=clock),
        FxRates(pairs, "CAD", ttl_s=1, clock=clock),
    )
    broker.quote("AAPL")
    clock.advance(600)
    pairs.mids["USDCAD"] = 1.38
    broker.quote("AAPL")
    clock.advance(300)
    pairs.mids["USDCAD"] = 1.39
    with pytest.raises(StaleQuote, match="unchanged"):
        broker.quote("AAPL")
