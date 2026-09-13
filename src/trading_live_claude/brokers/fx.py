"""Numeraire conversion: every price the paper book, sizer and risk gates see is in one currency.

Rates are USD spot pairs triangulated (X->CAD = X->USD / CAD->USD). Candles convert at the
current rate, a constant factor per series, so signals match native while price levels match quotes.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..logging_setup import get_logger
from ..venues import currency_of
from .base import Broker, BrokerError, StaleQuote
from .models import Account, Candle, Order, Position, Quote

if TYPE_CHECKING:
    from .ib import IBBroker

log = get_logger(__name__)

# Currencies IDEALPRO quotes as CCY.USD; everything else is USD.CCY.
_USD_QUOTED = frozenset({"EUR", "GBP", "AUD", "NZD"})

_QUOTE_PRICE_FIELDS = ("bidPrice", "askPrice", "lastTradePrice", "lastTradePriceTrHrs",
                       "openPrice", "highPrice", "lowPrice")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class FxUnavailable(BrokerError):
    """No rate fresh enough to convert this currency."""


class FxRates:
    def __init__(
        self,
        fetch_pair_mid: Callable[[str], float | None],
        numeraire: str,
        *,
        ttl_s: float = 60.0,
        max_age_s: float = 900.0,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._fetch = fetch_pair_mid
        self.numeraire = numeraire.upper()
        self.ttl_s = ttl_s
        self.max_age_s = max_age_s
        self._clock = clock
        self._cache: dict[str, tuple[float, datetime]] = {}

    def _pair_mid(self, pair: str) -> float:
        now = self._clock()
        cached = self._cache.get(pair)
        if cached is not None and (now - cached[1]).total_seconds() < self.ttl_s:
            return cached[0]
        try:
            mid = self._fetch(pair)
        except Exception as e:
            log.warning("fx.fetch_failed", pair=pair, error=str(e))
            mid = None
        if mid is not None and mid > 0:
            self._cache[pair] = (float(mid), now)
            return float(mid)
        if cached is not None and (now - cached[1]).total_seconds() <= self.max_age_s:
            return cached[0]
        raise FxUnavailable(f"no fresh {pair} rate")

    def _usd_per_unit(self, ccy: str) -> float:
        if ccy == "USD":
            return 1.0
        if ccy in _USD_QUOTED:
            return self._pair_mid(f"{ccy}USD")
        return 1.0 / self._pair_mid(f"USD{ccy}")

    def rate(self, ccy: str) -> float:
        """Numeraire units per one unit of ``ccy``."""
        ccy = ccy.upper()
        if ccy == self.numeraire:
            return 1.0
        return self._usd_per_unit(ccy) / self._usd_per_unit(self.numeraire)


def ib_spot_rates(ib: IBBroker, numeraire: str, *, ttl_s: float = 60.0,
                  max_age_s: float = 900.0) -> FxRates:
    from .ib import IBContract

    def fetch(pair: str) -> float | None:
        return ib.quote_contract(IBContract(symbol=pair, sec_type="forex")).mid

    return FxRates(fetch, numeraire, ttl_s=ttl_s, max_age_s=max_age_s)


class CurrencyNormalizingBroker:
    def __init__(
        self,
        inner: Broker,
        rates: FxRates,
        *,
        currency_for: Callable[[str], str] = currency_of,
    ) -> None:
        self.inner = inner
        self.rates = rates
        self.name = inner.name
        self.venue: str = getattr(inner, "venue", inner.name)
        self._currency_for = currency_for

    def _factor(self, symbol: str) -> float:
        try:
            return self.rates.rate(self._currency_for(symbol))
        except FxUnavailable as e:
            raise StaleQuote(symbol, (str(e),)) from e

    def _convert_quote(self, q: Quote) -> Quote:
        f = self._factor(q.symbol)
        if f == 1.0:
            return q
        return q.model_copy(update={k: getattr(q, k) * f for k in _QUOTE_PRICE_FIELDS
                                    if getattr(q, k) is not None})

    def quote(self, symbol: str) -> Quote:
        return self._convert_quote(self.inner.quote(symbol))

    def quotes(self, symbols: list[str]) -> list[Quote]:
        return [self._convert_quote(q) for q in self.inner.quotes(symbols)]

    def candles(self, symbol: str, start: datetime, end: datetime,
                interval: str = "OneDay") -> list[Candle]:
        bars = self.inner.candles(symbol, start, end, interval)
        f = self._factor(symbol) if bars else 1.0
        if f == 1.0:
            return bars
        return [b.model_copy(update={
            "open": b.open * f, "high": b.high * f, "low": b.low * f, "close": b.close * f,
            "VWAP": b.VWAP * f if b.VWAP is not None else None,
        }) for b in bars]

    def place_order(self, order: Order) -> Order:
        f = self._factor(order.symbol)
        if f != 1.0:
            order = order.model_copy(update={
                "limitPrice": order.limitPrice / f if order.limitPrice is not None else None,
                "stopPrice": order.stopPrice / f if order.stopPrice is not None else None,
            })
        return self.inner.place_order(order)

    # Account data stays in the broker's native currencies; only the paper book is converted.
    def accounts(self) -> list[Account]:
        return self.inner.accounts()

    def positions(self, account_number: str) -> list[Position]:
        return self.inner.positions(account_number)

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        return self.inner.equity(account_number, currency)

    def cancel_order(self, account_number: str, order_id: int) -> None:
        self.inner.cancel_order(account_number, order_id)
