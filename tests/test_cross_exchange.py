from __future__ import annotations

from trading_live_claude.microstructure import CrossExchangeArb, LimitOrderBook, XArbConfig
from trading_live_claude.microstructure.bitstamp_l2 import build_book, parse_book_message
from trading_live_claude.microstructure.orderbook import OrderBookLevel


def _book(bid: float, ask: float, size: float = 1.0) -> LimitOrderBook:
    return LimitOrderBook([OrderBookLevel(bid, size)], [OrderBookLevel(ask, size)])


def test_no_trade_before_second_venue() -> None:
    arb = CrossExchangeArb()
    tick = arb.update("kraken", _book(100.0, 100.1))
    assert tick.action == "none" and tick.n_trades == 0


def test_detects_and_captures_a_crossed_market() -> None:
    # Bitstamp is dear (bid 101 > Kraken ask 100.1): sell on bitstamp, buy on kraken.
    arb = CrossExchangeArb(cfg=XArbConfig(fee_bps=5.0, min_edge_bps=1.0, max_size=0.5))
    arb.update("kraken", _book(100.0, 100.1, size=1.0))
    tick = arb.update("bitstamp", _book(101.0, 101.1, size=1.0))
    assert tick.action == "sell_bitstamp_buy_kraken"
    assert tick.edge_bps > 0
    assert tick.size == 0.5                       # capped by max_size
    assert tick.trade_pnl > 0 and tick.cum_pnl == tick.trade_pnl
    assert tick.n_trades == 1


def test_aligned_books_yield_no_edge() -> None:
    arb = CrossExchangeArb(cfg=XArbConfig(fee_bps=10.0))
    arb.update("kraken", _book(100.00, 100.05))
    tick = arb.update("bitstamp", _book(100.01, 100.06))  # tiny gap, eaten by fees
    assert tick.action == "none"
    assert tick.edge_bps <= 1.0


def test_fees_gate_marginal_edges() -> None:
    cheap = CrossExchangeArb(cfg=XArbConfig(fee_bps=1.0, min_edge_bps=0.5))
    dear = CrossExchangeArb(cfg=XArbConfig(fee_bps=50.0, min_edge_bps=0.5))
    for arb in (cheap, dear):
        arb.update("kraken", _book(100.0, 100.1))
    t_cheap = cheap.update("bitstamp", _book(100.3, 100.4))
    t_dear = dear.update("bitstamp", _book(100.3, 100.4))
    assert t_cheap.action != "none"        # low fees -> the edge survives
    assert t_dear.action == "none"         # high fees -> same prices, no trade


def test_bitstamp_message_parsing() -> None:
    msg = {"event": "data", "channel": "order_book_btcusd",
           "data": {"bids": [["100.0", "1.5"], ["99.9", "2.0"]], "asks": [["100.1", "1.0"]]}}
    parsed = parse_book_message(msg)
    assert parsed is not None
    bids, asks = parsed
    lob = build_book(bids, asks, depth=10)
    assert lob.best_bid.price == 100.0 and lob.best_ask.price == 100.1
    assert parse_book_message({"event": "bts:subscription_succeeded"}) is None
