from __future__ import annotations

import numpy as np
import pytest

from trading_live_claude.microstructure import (
    ArbConfig,
    LimitOrderBook,
    OrderBookLevel,
    cross_market_arbitrage,
)
from trading_live_claude.microstructure.orderbook import order_flow_imbalance, synthetic_book


def test_microprice_between_touch_and_leans_to_thin_side() -> None:
    ob = LimitOrderBook([OrderBookLevel(99.9, 50.0)], [OrderBookLevel(100.1, 150.0)])
    assert ob.best_bid.price < ob.microprice < ob.best_ask.price
    assert ob.microprice < ob.mid          # ask is heavier -> microprice leans toward the bid
    assert ob.imbalance() < 0.0            # more size on the ask


def test_crossed_book_rejected() -> None:
    with pytest.raises(ValueError):
        LimitOrderBook([OrderBookLevel(100.2, 10.0)], [OrderBookLevel(100.1, 10.0)])
    with pytest.raises(ValueError):
        LimitOrderBook([], [OrderBookLevel(100.1, 10.0)])


def test_order_flow_imbalance_sign() -> None:
    prev = LimitOrderBook([OrderBookLevel(99.9, 50.0)], [OrderBookLevel(100.1, 100.0)])
    curr = LimitOrderBook([OrderBookLevel(100.0, 80.0)], [OrderBookLevel(100.1, 100.0)])  # bid ticks up
    assert order_flow_imbalance(prev, curr) > 0.0                                          # buying pressure


def test_synthetic_book_is_valid() -> None:
    ob = synthetic_book(mid=50.0, spread=0.04, bid_size=100, ask_size=100, depth=4,
                        rng=np.random.default_rng(0))
    assert len(ob.bids) == 4 and len(ob.asks) == 4
    assert ob.best_bid.price < ob.mid < ob.best_ask.price


def test_cross_market_arbitrage_captures_divergences() -> None:
    res = cross_market_arbitrage(ArbConfig(noise=0.15, entry_gap=0.15, exit_gap=0.03),
                                 steps=2000, rng=np.random.default_rng(1))
    assert res.n_trades > 0
    assert np.isfinite(res.pnl)
    assert res.mid_a.shape == (2001,) and res.gap.shape == (2001,)
    # A wider entry threshold trades less often.
    fewer = cross_market_arbitrage(ArbConfig(noise=0.15, entry_gap=0.40, exit_gap=0.03),
                                   steps=2000, rng=np.random.default_rng(1))
    assert fewer.n_trades < res.n_trades
