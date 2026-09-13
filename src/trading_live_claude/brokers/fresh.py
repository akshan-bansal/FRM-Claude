"""FreshQuoteBroker — wraps any Broker feed and stops stale quotes reaching fills, marks and heat.

Kraken/IB/IB Web never timestamp quotes, so "frozen" (fingerprint unchanged for ``max_frozen_s``)
is the only way to see those feeds stall; Questrade's halted/delay/lastTradeTime are used too.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from ..logging_setup import get_logger
from .base import Broker, StaleQuote
from .models import Account, Candle, Order, Position, Quote

if TYPE_CHECKING:
    from ..config.settings import Settings

log = get_logger(__name__)

StaleMode = Literal["warn", "raise"]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class FreshQuoteBroker:
    def __init__(
        self,
        inner: Broker,
        *,
        max_frozen_s: float | None = 900.0,
        max_trade_age_s: float | None = None,
        on_stale: StaleMode = "raise",
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.inner = inner
        self.name = inner.name
        self.venue: str = getattr(inner, "venue", inner.name)
        self.max_frozen_s = max_frozen_s
        self.max_trade_age_s = max_trade_age_s
        self.on_stale = on_stale
        self._clock = clock
        self._seen: dict[str, tuple[tuple[object, ...], datetime]] = {}
        self._stale_since: dict[str, datetime] = {}

    # ----- staleness ------------------------------------------------------

    def assess(self, q: Quote) -> tuple[str, ...]:
        """Reasons ``q`` is stale (empty when fresh). Records the observation for frozen tracking."""
        now = self._clock()
        reasons: list[str] = []
        if q.isHalted:
            reasons.append("halted")
        if q.delay:
            reasons.append(f"delayed {q.delay}m")
        if q.mid is None:
            reasons.append("no price")
        if q.bidPrice and q.askPrice and q.bidPrice > q.askPrice:
            reasons.append("crossed book")
        if self.max_trade_age_s is not None and q.lastTradeTime is not None:
            traded = q.lastTradeTime if q.lastTradeTime.tzinfo else q.lastTradeTime.replace(tzinfo=UTC)
            age = (now - traded).total_seconds()
            if age > self.max_trade_age_s:
                reasons.append(f"last trade {age:.0f}s old")

        fingerprint = (q.bidPrice, q.askPrice, q.lastTradePrice, q.bidSize, q.askSize, q.volume)
        prev = self._seen.get(q.symbol)
        first_seen = prev[1] if prev is not None and prev[0] == fingerprint else now
        self._seen[q.symbol] = (fingerprint, first_seen)
        unchanged_s = (now - first_seen).total_seconds()
        if self.max_frozen_s is not None and unchanged_s >= self.max_frozen_s:
            reasons.append(f"unchanged for {unchanged_s:.0f}s")
        return tuple(reasons)

    def is_stale(self, symbol: str) -> bool:
        return symbol in self._stale_since

    def _track(self, symbol: str, reasons: tuple[str, ...]) -> None:
        now = self._clock()
        if reasons:
            if symbol not in self._stale_since:
                self._stale_since[symbol] = now
                log.warning("quote.stale", symbol=symbol, venue=self.venue,
                            reasons=list(reasons), on_stale=self.on_stale)
        elif symbol in self._stale_since:
            since = self._stale_since.pop(symbol)
            log.info("quote.fresh_again", symbol=symbol, venue=self.venue,
                     stale_for_s=round((now - since).total_seconds()))

    # ----- quotes ---------------------------------------------------------

    def quote(self, symbol: str) -> Quote:
        q = self.inner.quote(symbol)
        reasons = self.assess(q)
        # Re-fetch once per stale episode only, so a feed frozen overnight doesn't double API calls.
        if reasons and not self.is_stale(q.symbol):
            q = self.inner.quote(symbol)
            reasons = self.assess(q)
        self._track(q.symbol, reasons)
        if reasons and self.on_stale == "raise":
            raise StaleQuote(q.symbol, reasons)
        return q

    def quotes(self, symbols: list[str]) -> list[Quote]:
        assessed = [(q, self.assess(q)) for q in self.inner.quotes(symbols)]
        retry = {q.symbol for q, r in assessed if r and not self.is_stale(q.symbol)}
        if retry:
            refetched = {q.symbol: q for q in self.inner.quotes(sorted(retry))}
            assessed = [
                (refetched[q.symbol], self.assess(refetched[q.symbol]))
                if q.symbol in retry and q.symbol in refetched else (q, r)
                for q, r in assessed
            ]
        stale: list[str] = []
        for q, r in assessed:
            self._track(q.symbol, r)
            if r:
                stale.append(f"{q.symbol} ({'; '.join(r)})")
        if stale and self.on_stale == "raise":
            raise StaleQuote(", ".join(q.symbol for q, r in assessed if r), tuple(stale))
        return [q for q, _ in assessed]

    # ----- pass-through ---------------------------------------------------

    def accounts(self) -> list[Account]:
        return self.inner.accounts()

    def positions(self, account_number: str) -> list[Position]:
        return self.inner.positions(account_number)

    def candles(self, symbol: str, start: datetime, end: datetime,
                interval: str = "OneDay") -> list[Candle]:
        return self.inner.candles(symbol, start, end, interval)

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        return self.inner.equity(account_number, currency)

    def place_order(self, order: Order) -> Order:
        return self.inner.place_order(order)

    def cancel_order(self, account_number: str, order_id: int) -> None:
        self.inner.cancel_order(account_number, order_id)


def guard_feed(feed: Broker, settings: Settings) -> Broker:
    """Wrap a paper feed per the ``stale_quote_*`` / ``on_stale_quote`` knobs; 0 disables a check."""
    return FreshQuoteBroker(
        feed,
        max_frozen_s=settings.stale_quote_frozen_s or None,
        max_trade_age_s=settings.stale_quote_max_trade_age_s or None,
        on_stale=settings.on_stale_quote,
    )
