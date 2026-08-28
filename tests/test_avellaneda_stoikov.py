from __future__ import annotations

import numpy as np

from trading_live_claude.microstructure import (
    ASParams,
    FillModel,
    MidPriceProcess,
    avellaneda_stoikov_quotes,
    simulate_market_making,
)


def test_reservation_price_skews_against_inventory() -> None:
    p = ASParams()
    _, _, res_long, _ = avellaneda_stoikov_quotes(100.0, 5.0, 0.5, p)
    _, _, res_flat, _ = avellaneda_stoikov_quotes(100.0, 0.0, 0.5, p)
    _, _, res_short, _ = avellaneda_stoikov_quotes(100.0, -5.0, 0.5, p)
    assert res_long < res_flat < res_short   # long inventory -> quote lower to sell it down
    assert res_flat == 100.0


def test_quotes_are_ordered_and_spread_positive() -> None:
    bid, ask, _, spread = avellaneda_stoikov_quotes(100.0, 0.0, 0.5, ASParams())
    assert bid < ask and spread > 0.0


def test_simulation_shapes() -> None:
    r = simulate_market_making(ASParams(), MidPriceProcess(), FillModel(), steps=150,
                               rng=np.random.default_rng(0))
    assert r.inventory.shape == (151,) and r.pnl.shape == (151,) and r.mid.shape == (151,)
    assert r.n_bid_fills >= 0 and r.n_ask_fills >= 0


def test_avellaneda_stoikov_controls_inventory_better_than_symmetric() -> None:
    """The whole point of A-S: skewing quotes by inventory keeps inventory tighter than a fixed
    symmetric spread on the same price paths."""
    p, mid, fm = ASParams(gamma=0.1, sigma=2.0, k=1.5), MidPriceProcess(sigma=2.0), FillModel(a=140, k=1.5)
    as_std, sym_std = [], []
    for seed in range(15):
        as_std.append(simulate_market_making(p, mid, fm, steps=200, rng=np.random.default_rng(seed)).inventory_std)
        sym_std.append(simulate_market_making(p, mid, fm, steps=200, rng=np.random.default_rng(seed),
                                              symmetric_half_spread=1.0).inventory_std)
    assert np.mean(as_std) < np.mean(sym_std)
