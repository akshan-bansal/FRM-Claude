"""Live Level-2 order book from Bitstamp's WebSocket v2 ``order_book`` channel.

The second venue for cross-exchange arbitrage (pairs with :mod:`.kraken_l2`). Unlike Kraken's
snapshot-plus-deltas feed, Bitstamp's ``order_book_{pair}`` channel pushes a **full top-100
snapshot on every change** — no delta maintenance, no checksum — so each message rebuilds the book
directly. Public, no authentication. Same :class:`BookUpdate` shape as the Kraken feed, so both
plug into the same downstream code.

Reference: https://www.bitstamp.net/websocket/v2/
"""
from __future__ import annotations

import json
from collections.abc import Callable

from .kraken_l2 import BookUpdate
from .orderbook import LimitOrderBook, OrderBookLevel, order_flow_imbalance

BITSTAMP_WS_V2 = "wss://ws.bitstamp.net"


def parse_book_message(msg: dict[str, object]) -> tuple[list[list[str]], list[list[str]]] | None:
    """Return ``(bids, asks)`` as lists of ``[price, qty]`` string pairs for a ``data`` event, else
    ``None`` (subscription acks, reconnect requests, heartbeats)."""
    if msg.get("event") != "data":
        return None
    data = msg.get("data")
    if not isinstance(data, dict):
        return None
    bids = data.get("bids")
    asks = data.get("asks")
    if not bids or not asks:
        return None
    return bids, asks


def build_book(bids: list[list[str]], asks: list[list[str]], depth: int) -> LimitOrderBook:
    """Top-``depth`` :class:`LimitOrderBook` from Bitstamp ``[price, qty]`` string pairs."""
    b = [OrderBookLevel(float(p), float(q)) for p, q in bids[:depth]]
    a = [OrderBookLevel(float(p), float(q)) for p, q in asks[:depth]]
    return LimitOrderBook(b, a)


def subscribe_message(pair: str) -> str:
    return json.dumps({"event": "bts:subscribe", "data": {"channel": f"order_book_{pair}"}})


async def stream_order_book(pair: str, *, on_update: Callable[[BookUpdate], None], depth: int = 10,
                            url: str = BITSTAMP_WS_V2, max_messages: int | None = None) -> None:
    """Connect to Bitstamp, subscribe to ``order_book_{pair}`` (e.g. ``btcusd``), and call
    ``on_update(BookUpdate)`` on each snapshot. Needs the optional ``l2`` extra (``websockets``)."""
    import websockets

    prev_top: LimitOrderBook | None = None
    seen = 0
    async with websockets.connect(url, max_size=2**22) as ws:
        await ws.send(subscribe_message(pair))
        async for raw in ws:
            parsed = parse_book_message(json.loads(raw))
            if parsed is None:
                continue
            bids, asks = parsed
            try:
                lob = build_book(bids, asks, depth)
            except ValueError:
                continue
            ofi = order_flow_imbalance(prev_top, lob) if prev_top is not None else 0.0
            on_update(BookUpdate(symbol=pair, book=lob, microprice=lob.microprice,
                                 imbalance=lob.imbalance(), ofi=ofi, checksum_ok=True))
            prev_top = lob
            seen += 1
            if max_messages is not None and seen >= max_messages:
                return
