"""VenueRoutedFeed — one Broker surface over several feeds, dispatched by each symbol's venue."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from ..venues import venue_for
from .base import Broker
from .models import Account, Candle, Order, Position, Quote


class VenueRoutedFeed:
    name = "global"
    venue = "global"

    def __init__(self, feeds: Mapping[str, Broker], default: Broker) -> None:
        """``feeds`` maps a venue code (e.g. ``CRYPTO``) to its feed; other venues use ``default``."""
        self.feeds = dict(feeds)
        self.default = default

    def feed_for(self, symbol: str) -> Broker:
        return self.feeds.get(venue_for(symbol)[0].code, self.default)

    def quote(self, symbol: str) -> Quote:
        return self.feed_for(symbol).quote(symbol)

    def quotes(self, symbols: list[str]) -> list[Quote]:
        groups: dict[int, tuple[Broker, list[str]]] = {}
        for s in symbols:
            feed = self.feed_for(s)
            groups.setdefault(id(feed), (feed, []))[1].append(s)
        by_symbol: dict[str, Quote] = {}
        for feed, syms in groups.values():
            by_symbol.update({q.symbol: q for q in feed.quotes(syms)})
        return [by_symbol[s] for s in symbols if s in by_symbol]

    def candles(self, symbol: str, start: datetime, end: datetime,
                interval: str = "OneDay") -> list[Candle]:
        return self.feed_for(symbol).candles(symbol, start, end, interval)

    def place_order(self, order: Order) -> Order:
        return self.feed_for(order.symbol).place_order(order)

    def cancel_order(self, account_number: str, order_id: int) -> None:
        self.default.cancel_order(account_number, order_id)

    def accounts(self) -> list[Account]:
        return self.default.accounts()

    def positions(self, account_number: str) -> list[Position]:
        return self.default.positions(account_number)

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        return self.default.equity(account_number, currency)
