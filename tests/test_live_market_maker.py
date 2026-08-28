from __future__ import annotations

import numpy as np

from trading_live_claude.microstructure import (
    BookUpdate,
    LimitOrderBook,
    MMConfig,
    PaperMarketMaker,
)
from trading_live_claude.microstructure.orderbook import OrderBookLevel


def _update(micro: float, ofi: float = 0.0) -> BookUpdate:
    book = LimitOrderBook([OrderBookLevel(micro - 0.05, 1.0)], [OrderBookLevel(micro + 0.05, 1.0)])
    return BookUpdate(symbol="BTC/USD", book=book, microprice=micro, imbalance=0.0, ofi=ofi, checksum_ok=True)


def _warm(mm: PaperMarketMaker) -> None:
    for m in (100.0, 101.0, 99.5, 100.5, 100.0, 101.5):  # give the sigma estimate something to chew
        mm._mids.append(m)


def test_reservation_skews_against_inventory() -> None:
    mm = PaperMarketMaker(cfg=MMConfig(gamma=0.2))
    _warm(mm)
    mm.inventory = 0.01                      # long
    _, _, res_long, _ = mm.quotes(100.0, 0.0)
    mm.inventory = -0.01                     # short
    _, _, res_short, _ = mm.quotes(100.0, 0.0)
    assert res_long < 100.0 < res_short      # long -> quote lower to sell down; short -> higher


def test_ofi_leans_the_reservation() -> None:
    mm = PaperMarketMaker()
    _warm(mm)
    _, _, res_flat, _ = mm.quotes(100.0, 0.0)
    _, _, res_buy, _ = mm.quotes(100.0, 5.0)   # buying pressure
    assert res_buy > res_flat


def test_quotes_ordered_and_spread_positive() -> None:
    mm = PaperMarketMaker(cfg=MMConfig(min_half_spread=0.01))
    _warm(mm)
    bid, ask, _, half = mm.quotes(100.0, 0.0)
    assert bid < ask and half >= 0.01


def test_inventory_stays_within_limit() -> None:
    cfg = MMConfig(inventory_limit=0.02, quote_size=0.001, fill_dt=0.5)  # hot fills
    mm = PaperMarketMaker(cfg=cfg, rng=np.random.default_rng(1))
    rng = np.random.default_rng(2)
    for _ in range(400):
        mm.on_book(_update(100.0 + rng.normal(0, 0.2)))
    assert abs(mm.inventory) <= cfg.inventory_limit + 1e-9


def test_paper_maker_tracks_state_and_never_raises() -> None:
    mm = PaperMarketMaker(rng=np.random.default_rng(3))
    rng = np.random.default_rng(4)
    last = None
    for _ in range(200):
        last = mm.on_book(_update(100.0 + rng.normal(0, 0.1), ofi=rng.normal(0, 1)))
    assert last is not None
    assert last.step == 200
    assert np.isfinite(last.pnl) and last.fills >= 0
    assert last.bid < last.ask
