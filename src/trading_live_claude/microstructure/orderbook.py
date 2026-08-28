"""A synthetic L2 limit order book and the two classic microstructure signals it exposes.

* **microprice** — a size-weighted mid, ``(bid_px*ask_sz + ask_px*bid_sz)/(bid_sz+ask_sz)``. It
  leans toward the side with *less* size, which is where the price is more likely to move, so it
  is a better short-horizon fair value than the plain mid.
* **queue imbalance** — ``(bid_sz - ask_sz)/(bid_sz + ask_sz)`` at the top of book, a well-known
  predictor of the next tick's direction.
* **order-flow imbalance (OFI)** — the Cont-Kukanov-Stoikov flow between two book snapshots, which
  attributes mid-price moves to net signed size arriving at the touch.

Book depth is real (multiple levels), so these are computed the way an exchange feed would give
them. No live venue is wired — this is the simulated substrate the execution families trade on.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    size: float


class LimitOrderBook:
    """Bids (descending price) and asks (ascending price). Levels are ``OrderBookLevel``."""

    def __init__(self, bids: list[OrderBookLevel], asks: list[OrderBookLevel]) -> None:
        if not bids or not asks:
            raise ValueError("book needs at least one bid and one ask level")
        self.bids = sorted(bids, key=lambda lvl: -lvl.price)
        self.asks = sorted(asks, key=lambda lvl: lvl.price)
        if self.bids[0].price >= self.asks[0].price:
            raise ValueError("crossed book: best bid >= best ask")

    @property
    def best_bid(self) -> OrderBookLevel:
        return self.bids[0]

    @property
    def best_ask(self) -> OrderBookLevel:
        return self.asks[0]

    @property
    def mid(self) -> float:
        return 0.5 * (self.best_bid.price + self.best_ask.price)

    @property
    def spread(self) -> float:
        return self.best_ask.price - self.best_bid.price

    @property
    def microprice(self) -> float:
        bp, bs = self.best_bid.price, self.best_bid.size
        ap, as_ = self.best_ask.price, self.best_ask.size
        return (bp * as_ + ap * bs) / (bs + as_)

    def imbalance(self, levels: int = 1) -> float:
        """Queue imbalance in [-1, 1] over the top ``levels`` (positive = bid-heavy)."""
        bid_sz = sum(lvl.size for lvl in self.bids[:levels])
        ask_sz = sum(lvl.size for lvl in self.asks[:levels])
        total = bid_sz + ask_sz
        return float((bid_sz - ask_sz) / total) if total > 0 else 0.0


def order_flow_imbalance(prev: LimitOrderBook, curr: LimitOrderBook) -> float:
    """Cont-Kukanov-Stoikov OFI between two consecutive top-of-book snapshots.

    Bid contributes +size when the bid price rises (or +delta-size when it holds), -size when it
    falls; the ask contributes with the opposite sign. Positive OFI = net buying pressure.
    """
    e_bid = _side_flow(prev.best_bid, curr.best_bid, is_bid=True)
    e_ask = _side_flow(prev.best_ask, curr.best_ask, is_bid=False)
    return float(e_bid - e_ask)


def _side_flow(prev: OrderBookLevel, curr: OrderBookLevel, *, is_bid: bool) -> float:
    up = curr.price > prev.price
    down = curr.price < prev.price
    if is_bid:
        if up:
            return curr.size
        if down:
            return -prev.size
        return curr.size - prev.size
    # ask side mirrors
    if down:
        return curr.size
    if up:
        return -prev.size
    return curr.size - prev.size


def synthetic_book(mid: float, spread: float, bid_size: float, ask_size: float,
                   depth: int = 5, rng: np.random.Generator | None = None) -> LimitOrderBook:
    """Build a plausible multi-level book around ``mid`` with a given touch size on each side."""
    rng = rng or np.random.default_rng()
    tick = spread / 2.0
    bids = [OrderBookLevel(mid - tick * (i + 1), bid_size * float(rng.uniform(0.7, 1.3))) for i in range(depth)]
    asks = [OrderBookLevel(mid + tick * (i + 1), ask_size * float(rng.uniform(0.7, 1.3))) for i in range(depth)]
    return LimitOrderBook(bids, asks)
