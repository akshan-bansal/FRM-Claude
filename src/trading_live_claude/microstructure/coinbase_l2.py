"""Live Level-2 order book from Coinbase Exchange's WebSocket ``level2_batch`` channel.

A third live venue (pairs with :mod:`.kraken_l2` for Coinbase⇄Kraken cross-exchange arb). Coinbase
Exchange's ``level2``/``level2_batch`` market-data channels are public — no authentication — and
stream a ``snapshot`` followed by ``l2update`` deltas (``["side", price, new_size]``; size 0
removes the level). Same :class:`BookUpdate` shape as the other feeds, so it plugs into the same
cross-exchange engine.

Reference: https://docs.cdp.coinbase.com/exchange/websocket-feed/overview
"""
from __future__ import annotations

import json
from collections.abc import Callable

from .kraken_l2 import BookUpdate
from .orderbook import LimitOrderBook, OrderBookLevel, order_flow_imbalance

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"


class CoinbaseOrderBook:
    """Maintains one product's book from a Coinbase ``snapshot`` + ``l2update`` deltas."""

    def __init__(self, depth: int = 10) -> None:
        self.depth = depth
        self._bids: dict[str, str] = {}
        self._asks: dict[str, str] = {}

    def apply_snapshot(self, bids: list[list[str]], asks: list[list[str]]) -> None:
        self._bids = {str(p): str(s) for p, s in bids}
        self._asks = {str(p): str(s) for p, s in asks}
        self._truncate()

    def apply_l2update(self, changes: list[list[str]]) -> None:
        for side, price, size in changes:
            book = self._bids if side == "buy" else self._asks
            if float(size) == 0.0:
                book.pop(str(price), None)
            else:
                book[str(price)] = str(size)
        self._truncate()

    def _truncate(self) -> None:
        self._bids = dict(sorted(self._bids.items(), key=lambda kv: -float(kv[0]))[:self.depth])
        self._asks = dict(sorted(self._asks.items(), key=lambda kv: float(kv[0]))[:self.depth])

    def to_limit_order_book(self) -> LimitOrderBook:
        bids = [OrderBookLevel(float(p), float(s)) for p, s in sorted(self._bids.items(), key=lambda kv: -float(kv[0]))]
        asks = [OrderBookLevel(float(p), float(s)) for p, s in sorted(self._asks.items(), key=lambda kv: float(kv[0]))]
        return LimitOrderBook(bids, asks)


def subscribe_message(product: str) -> str:
    return json.dumps({"type": "subscribe", "product_ids": [product], "channels": ["level2_batch"]})


async def stream_order_book(product: str, *, on_update: Callable[[BookUpdate], None], depth: int = 10,
                            url: str = COINBASE_WS, max_messages: int | None = None) -> None:
    """Connect to Coinbase Exchange, subscribe to ``level2_batch`` for ``product`` (e.g. ``BTC-USD``),
    and call ``on_update(BookUpdate)`` per book message. Needs the optional ``l2`` extra."""
    import websockets

    book = CoinbaseOrderBook(depth=depth)
    prev_top: LimitOrderBook | None = None
    seen = 0
    async with websockets.connect(url, max_size=2**22) as ws:
        await ws.send(subscribe_message(product))
        async for raw in ws:
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype == "snapshot":
                book.apply_snapshot(msg.get("bids", []), msg.get("asks", []))
            elif mtype == "l2update":
                book.apply_l2update(msg.get("changes", []))
            else:
                continue  # subscriptions ack, heartbeat, errors
            try:
                lob = book.to_limit_order_book()
            except ValueError:
                continue
            ofi = order_flow_imbalance(prev_top, lob) if prev_top is not None else 0.0
            on_update(BookUpdate(symbol=product, book=lob, microprice=lob.microprice,
                                 imbalance=lob.imbalance(), ofi=ofi, checksum_ok=True))
            prev_top = lob
            seen += 1
            if max_messages is not None and seen >= max_messages:
                return
