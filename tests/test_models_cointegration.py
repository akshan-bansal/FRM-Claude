from __future__ import annotations

import numpy as np

from trading_live_claude.models import engle_granger, half_life


def test_engle_granger_detects_cointegration() -> None:
    rng = np.random.default_rng(3)
    n = 600
    x = np.cumsum(rng.normal(0, 1, n)) + 100.0
    spread = np.zeros(n)
    for t in range(1, n):
        spread[t] = 0.85 * spread[t - 1] + rng.normal(0, 0.5)  # stationary OU
    y = 2.0 + 1.5 * x + spread
    res = engle_granger(y, x)
    assert res.cointegrated and res.tradeable
    assert res.pvalue < 0.05
    assert abs(res.hedge_ratio - 1.5) < 0.2
    assert 0.0 < res.half_life < 60.0


def test_engle_granger_rejects_independent_random_walks() -> None:
    rng = np.random.default_rng(4)
    n = 600
    x = np.cumsum(rng.normal(0, 1, n))
    y = np.cumsum(rng.normal(0, 1, n))  # independent -> not cointegrated
    res = engle_granger(y, x)
    assert not res.cointegrated
    assert res.pvalue > 0.05


def test_half_life_short_for_reverting_long_for_walk() -> None:
    """An OU process reverts fast (small half-life); a random walk either never reverts (inf) or,
    on a finite sample, shows a spuriously long one. The gap is what matters, not an exact inf."""
    rng = np.random.default_rng(5)
    n = 500
    ou = np.zeros(n)
    for t in range(1, n):
        ou[t] = 0.8 * ou[t - 1] + rng.normal(0, 1)  # strong reversion -> half-life ~3 bars
    hl_ou = half_life(ou)
    assert np.isfinite(hl_ou) and 0.0 < hl_ou < 15.0
    hl_walk = half_life(np.cumsum(rng.normal(0, 1, n)))
    assert hl_walk > 10.0 * hl_ou  # a walk reverts far more slowly (or not at all)
