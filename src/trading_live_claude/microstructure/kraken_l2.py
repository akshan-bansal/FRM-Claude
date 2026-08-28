"""Live Level-2 order book from Kraken's WebSocket v2 ``book`` channel.

This replaces the synthetic :class:`~trading_live_claude.microstructure.orderbook.LimitOrderBook`
with a *real* depth-of-book feed: Kraken streams full L2 depth for free, and crypto is a venue
where two-sided quoting (Avellaneda-Stoikov) and cross-exchange arbitrage are actually executable.

The design mirrors the rest of the repo's I/O modules — a pure, synchronous, fully-tested core
(:class:`KrakenOrderBook` maintains the book from snapshot + update messages and computes the CRC32
integrity checksum) wrapped by a thin async transport (:func:`stream_order_book`) that lazily
imports ``websockets`` (the optional ``l2`` extra). Prices and quantities are kept as the exact
wire strings — Kraken's checksum is defined on those, and floats would lose precision — and only
converted to float when building a :class:`LimitOrderBook`.

Reference: https://docs.kraken.com/api/docs/websocket-v2/book/ and the checksum guide
https://docs.kraken.com/api/docs/guides/spot-ws-book-v2/
"""
from __future__ import annotations

import json
import zlib
from collections.abc import Callable
from dataclasses import dataclass

from .orderbook import LimitOrderBook, OrderBookLevel, order_flow_imbalance

Level = dict[str, object]

KRAKEN_WS_V2 = "wss://ws.kraken.com/v2"


def _checksum_token(raw: str) -> str:
    """Kraken checksum formatting for one price/qty: drop the decimal point, strip leading zeros."""
    return raw.replace(".", "").lstrip("0") or "0"


@dataclass(frozen=True)
class BookUpdate:
    """One post-update snapshot handed to the ``on_update`` callback."""

    symbol: str
    book: LimitOrderBook
    microprice: float
    imbalance: float
    ofi: float          # order-flow imbalance vs the previous top of book (0.0 on the first update)
    checksum_ok: bool   # whether the local book's CRC32 matched Kraken's (None-checksum -> True)


class KrakenOrderBook:
    """Maintains one symbol's L2 book from Kraken v2 ``book`` snapshot/update messages.

    Levels are stored as ``{price_string: qty_string}`` using the exact wire strings, so the CRC32
    checksum (defined over those strings) is reproducible. ``depth`` truncates each side after every
    update; the checksum always uses the top 10 regardless of ``depth``.
    """

    def __init__(self, depth: int = 10) -> None:
        self.depth = depth
        self._bids: dict[str, str] = {}
        self._asks: dict[str, str] = {}

    @staticmethod
    def _apply(side: dict[str, str], levels: list[dict[str, object]]) -> None:
        for lvl in levels:
            price = str(lvl["price"])
            qty = str(lvl["qty"])
            if float(qty) == 0.0:
                side.pop(price, None)      # qty 0 removes the level
            else:
                side[price] = qty

    def _truncate(self) -> None:
        self._bids = dict(sorted(self._bids.items(), key=lambda kv: -float(kv[0]))[:self.depth])
        self._asks = dict(sorted(self._asks.items(), key=lambda kv: float(kv[0]))[:self.depth])

    def apply_snapshot(self, bids: list[dict[str, object]], asks: list[dict[str, object]]) -> None:
        self._bids = {}
        self._asks = {}
        self._apply(self._bids, bids)
        self._apply(self._asks, asks)
        self._truncate()

    def apply_update(self, bids: list[dict[str, object]], asks: list[dict[str, object]]) -> None:
        self._apply(self._bids, bids)
        self._apply(self._asks, asks)
        self._truncate()

    def _sorted(self, side: dict[str, str], *, ascending: bool) -> list[tuple[str, str]]:
        return sorted(side.items(), key=lambda kv: (1 if ascending else -1) * float(kv[0]))

    def checksum(self) -> int:
        """CRC32 of the top-10 asks (low->high) then top-10 bids (high->low), per Kraken's spec."""
        asks = self._sorted(self._asks, ascending=True)[:10]
        bids = self._sorted(self._bids, ascending=False)[:10]
        payload = "".join(_checksum_token(p) + _checksum_token(q) for p, q in asks)
        payload += "".join(_checksum_token(p) + _checksum_token(q) for p, q in bids)
        return zlib.crc32(payload.encode("ascii")) & 0xFFFFFFFF

    def to_limit_order_book(self) -> LimitOrderBook:
        """Convert the current book to a :class:`LimitOrderBook` (floats). Raises if a side is empty."""
        bids = [OrderBookLevel(float(p), float(q)) for p, q in self._sorted(self._bids, ascending=False)]
        asks = [OrderBookLevel(float(p), float(q)) for p, q in self._sorted(self._asks, ascending=True)]
        return LimitOrderBook(bids, asks)


def parse_book_message(msg: dict[str, object]) -> tuple[str, str, list[Level], list[Level], int | None] | None:
    """Normalize a raw v2 message to ``(type, symbol, bids, asks, checksum)`` for ``book`` data
    messages; return ``None`` for status/heartbeat/ack and anything else."""
    if msg.get("channel") != "book" or msg.get("type") not in ("snapshot", "update"):
        return None
    data = msg.get("data")
    if not isinstance(data, list) or not data:
        return None
    d = data[0]
    return (str(msg["type"]), str(d.get("symbol", "")), d.get("bids", []) or [],
            d.get("asks", []) or [], d.get("checksum"))


def subscribe_message(symbol: str, depth: int) -> str:
    return json.dumps({"method": "subscribe", "params": {"channel": "book", "symbol": [symbol], "depth": depth}})


async def stream_order_book(symbol: str, *, depth: int = 10, on_update: Callable[[BookUpdate], None],
                            url: str = KRAKEN_WS_V2, max_messages: int | None = None,
                            validate_checksum: bool = True) -> None:
    """Connect to Kraken v2, subscribe to ``symbol``'s book, and call ``on_update(BookUpdate)`` on
    each snapshot/update. Stops after ``max_messages`` book messages (``None`` = run forever).

    Needs the optional ``l2`` extra (``websockets``). On a checksum mismatch it flags the update
    (``checksum_ok=False``) so the caller can decide to resync; it does not silently trust the book.
    """
    import websockets  # optional 'l2' extra; imported lazily

    book = KrakenOrderBook(depth=depth)
    prev_top: LimitOrderBook | None = None
    seen = 0
    async with websockets.connect(url, max_size=2**22) as ws:
        await ws.send(subscribe_message(symbol, depth))
        async for raw in ws:
            parsed = parse_book_message(json.loads(raw, parse_float=str, parse_int=str))
            if parsed is None:
                continue
            mtype, sym, bids, asks, checksum = parsed
            if mtype == "snapshot":
                book.apply_snapshot(bids, asks)
            else:
                book.apply_update(bids, asks)
            try:
                lob = book.to_limit_order_book()
            except ValueError:
                continue  # one side momentarily empty; wait for the next message
            ok = True
            if validate_checksum and checksum is not None:
                ok = book.checksum() == int(checksum)
            ofi = order_flow_imbalance(prev_top, lob) if prev_top is not None else 0.0
            on_update(BookUpdate(symbol=sym or symbol, book=lob, microprice=lob.microprice,
                                 imbalance=lob.imbalance(), ofi=ofi, checksum_ok=ok))
            prev_top = lob
            seen += 1
            if max_messages is not None and seen >= max_messages:
                return
