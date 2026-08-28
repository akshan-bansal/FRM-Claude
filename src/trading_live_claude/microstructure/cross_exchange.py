"""Cross-exchange arbitrage across two live order books (Kraken + Bitstamp).

Holds the latest book from each venue and, on every update, checks both directions — sell on one
venue's bid while buying the other's ask — for an edge that clears taker fees on both legs. When it
does, it paper-captures ``min(top-of-book size, max_size)`` and books the spread minus fees.

**Paper only.** Nothing here posts an order. And note the standing real-world caveat: genuine
cross-exchange arb needs pre-funded balances on *both* venues (you can't teleport coins between
them mid-trade) and latency low enough to hit the quote before it moves — so this measures the
*opportunity*, and the paper P&L is an upper bound on what a funded, co-located setup could capture.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from itertools import permutations
from typing import Any, cast

from .kraken_l2 import BookUpdate
from .orderbook import LimitOrderBook

_StreamFn = Callable[..., Coroutine[Any, Any, None]]


@dataclass(frozen=True)
class XArbConfig:
    fee_bps: float = 10.0        # per-leg taker fee, basis points (0.10%)
    min_edge_bps: float = 1.0    # require net edge beyond this to "trade"
    max_size: float = 0.01       # cap on base size per arbitrage


@dataclass(frozen=True)
class XArbTick:
    venue_updated: str
    edge_bps: float              # best net cross-venue edge, bps (<= 0 means no opportunity)
    action: str                  # "sell_{v}_buy_{v}" when captured, else "none"
    size: float
    trade_pnl: float             # realized on this tick (quote-ccy)
    cum_pnl: float
    n_trades: int
    quotes: dict[str, tuple[float, float]]  # venue -> (best_bid, best_ask)


@dataclass
class CrossExchangeArb:
    """Two-venue latency-arb detector. Feed it ``update(venue, book)``; it returns an XArbTick."""

    cfg: XArbConfig = field(default_factory=XArbConfig)
    cum_pnl: float = 0.0
    n_trades: int = 0
    _books: dict[str, LimitOrderBook] = field(default_factory=dict)

    def update(self, venue: str, book: LimitOrderBook) -> XArbTick:
        self._books[venue] = book
        quotes = {v: (b.best_bid.price, b.best_ask.price) for v, b in self._books.items()}
        if len(self._books) < 2:
            return XArbTick(venue, 0.0, "none", 0.0, 0.0, self.cum_pnl, self.n_trades, quotes)

        fee = self.cfg.fee_bps / 1e4
        best = None  # (edge_bps, sell_v, buy_v, bid, ask, size)
        for sell_v, buy_v in permutations(self._books, 2):
            sell_b, buy_b = self._books[sell_v], self._books[buy_v]
            bid, ask = sell_b.best_bid.price, buy_b.best_ask.price
            mid = 0.5 * (bid + ask)
            net_per_unit = (bid - ask) - fee * (bid + ask)   # sell high bid, buy low ask, both legs charged
            edge_bps = net_per_unit / mid * 1e4
            avail = min(sell_b.best_bid.size, buy_b.best_ask.size)
            if best is None or edge_bps > best[0]:
                best = (edge_bps, sell_v, buy_v, bid, ask, avail)

        assert best is not None  # >=2 venues -> permutations non-empty, so best is always set
        edge_bps, sell_v, buy_v, bid, ask, avail = best
        action, size, trade_pnl = "none", 0.0, 0.0
        if edge_bps > self.cfg.min_edge_bps:
            size = min(avail, self.cfg.max_size)
            trade_pnl = ((bid - ask) - fee * (bid + ask)) * size
            self.cum_pnl += trade_pnl
            self.n_trades += 1
            action = f"sell_{sell_v}_buy_{buy_v}"
        return XArbTick(venue, edge_bps, action, size, trade_pnl, self.cum_pnl, self.n_trades, quotes)


class _StopStreaming(Exception):
    """Internal: raised from a callback to break both venue streams once the tick budget is hit."""


# Per-venue default BTC/USD symbol in that venue's own notation.
VENUE_DEFAULT_SYMBOL: dict[str, str] = {"kraken": "BTC/USD", "bitstamp": "btcusd", "coinbase": "BTC-USD"}


def _venue_stream(venue: str) -> _StreamFn:
    """The async ``stream_order_book`` for a venue (lazy import; all share the same call shape)."""
    if venue == "kraken":
        from .kraken_l2 import stream_order_book as _k
        return cast(_StreamFn, _k)
    if venue == "bitstamp":
        from .bitstamp_l2 import stream_order_book as _b
        return cast(_StreamFn, _b)
    if venue == "coinbase":
        from .coinbase_l2 import stream_order_book as _c
        return cast(_StreamFn, _c)
    raise ValueError(f"Unknown venue {venue!r}. Known: {sorted(VENUE_DEFAULT_SYMBOL)}")


async def run_cross_exchange_arb(*, on_tick: Callable[[XArbTick], None],
                                 venues: tuple[str, str] = ("kraken", "bitstamp"),
                                 symbols: dict[str, str] | None = None, cfg: XArbConfig | None = None,
                                 depth: int = 10, max_ticks: int | None = None) -> CrossExchangeArb:
    """Stream two live books concurrently and run the arb detector across them.

    ``venues`` is any two of ``kraken``/``bitstamp``/``coinbase``; ``symbols`` overrides the
    per-venue BTC/USD default (each venue uses its own notation). Paper only — public market data,
    no orders. Returns the engine for final P&L/trade counts. Needs the optional ``l2`` extra.
    """
    if len(venues) != 2 or venues[0] == venues[1]:
        raise ValueError(f"need exactly two distinct venues, got {venues!r}")
    sym = dict(symbols or {})
    engine = CrossExchangeArb(cfg=cfg or XArbConfig())
    count = 0

    def make_cb(venue: str) -> Callable[[BookUpdate], None]:
        def _cb(u: BookUpdate) -> None:
            nonlocal count
            tick = engine.update(venue, u.book)
            on_tick(tick)
            # Only count once both venues are live, so a fast feed can't exhaust the budget before
            # the slower venue connects (otherwise we'd stop with a single book and never compare).
            if len(tick.quotes) >= 2:
                count += 1
                if max_ticks is not None and count >= max_ticks:
                    raise _StopStreaming
        return _cb

    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(_venue_stream(v)(sym.get(v, VENUE_DEFAULT_SYMBOL[v]), depth=depth, on_update=make_cb(v)))
        for v in venues
    ]
    try:
        await asyncio.gather(*tasks)
    except _StopStreaming:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return engine
