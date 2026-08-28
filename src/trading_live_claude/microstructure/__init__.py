"""Microstructure & stochastic-control layer (execution families), all against a **simulated**
market — Questrade is EOD/quote-only with no L2 depth or market-making venue, so nothing here can
run live until a real order-book feed and a quoting venue exist.

* :mod:`.simulator` — a mid-price process and a Poisson limit-order fill model.
* :mod:`.avellaneda_stoikov` — the Avellaneda-Stoikov optimal market-making policy + a symmetric
  baseline, and a driver that runs a policy through the simulator tracking inventory and P&L.
* :mod:`.orderbook` — a synthetic L2 limit order book with microprice and order-flow imbalance.
* :mod:`.arbitrage` — two correlated venues and a cross-market arbitrage capture simulation.
"""
from __future__ import annotations

from .arbitrage import ArbConfig, ArbResult, cross_market_arbitrage
from .avellaneda_stoikov import (
    ASParams,
    MarketMakingResult,
    avellaneda_stoikov_quotes,
    simulate_market_making,
)
from .cross_exchange import CrossExchangeArb, XArbConfig, XArbTick, run_cross_exchange_arb
from .kraken_l2 import BookUpdate, KrakenOrderBook, parse_book_message, stream_order_book
from .live_market_maker import MMConfig, MMState, PaperMarketMaker, run_paper_market_maker
from .orderbook import LimitOrderBook, OrderBookLevel
from .simulator import FillModel, MidPriceProcess

__all__ = [
    "ASParams",
    "ArbConfig",
    "ArbResult",
    "BookUpdate",
    "CrossExchangeArb",
    "FillModel",
    "KrakenOrderBook",
    "LimitOrderBook",
    "MMConfig",
    "MMState",
    "MarketMakingResult",
    "MidPriceProcess",
    "OrderBookLevel",
    "PaperMarketMaker",
    "XArbConfig",
    "XArbTick",
    "avellaneda_stoikov_quotes",
    "cross_market_arbitrage",
    "parse_book_message",
    "run_cross_exchange_arb",
    "run_paper_market_maker",
    "simulate_market_making",
    "stream_order_book",
]
