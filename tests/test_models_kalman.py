from __future__ import annotations

import numpy as np
import pytest

from trading_live_claude.models import KalmanHedge


def _cointegrated(seed: int = 1, n: int = 600, beta: float = 1.8):
    rng = np.random.default_rng(seed)
    x = np.cumsum(rng.normal(0, 1, n)) + 100.0
    spread = np.zeros(n)
    for t in range(1, n):
        spread[t] = 0.9 * spread[t - 1] + rng.normal(0, 0.5)
    y = 3.0 + beta * x + spread
    return y, x


def test_kalman_recovers_the_hedge_ratio() -> None:
    y, x = _cointegrated(beta=1.8)
    state = KalmanHedge(delta=1e-3).filter(y, x)
    # The filtered ratio should converge near the true 1.8 by the end of the sample.
    assert abs(state.beta[-1] - 1.8) < 0.3


def test_kalman_is_causal_prefix_stable() -> None:
    """Filtering a prefix must give the exact same states as the full run truncated — the filter
    is a pure forward recursion, so state_t depends only on data through t (no lookahead)."""
    y, x = _cointegrated()
    full = KalmanHedge().filter(y, x)
    part = KalmanHedge().filter(y[:400], x[:400])
    assert np.allclose(full.beta[:400], part.beta, atol=1e-12)
    assert np.allclose(full.spread[:400], part.spread, atol=1e-12)


def test_kalman_output_shapes_and_finiteness() -> None:
    y, x = _cointegrated(n=300)
    st = KalmanHedge(warmup=25).filter(y, x)
    for arr in (st.alpha, st.beta, st.spread, st.spread_std, st.zscore):
        assert arr.shape == (300,)
        assert np.isfinite(arr).all()
    assert st.warmup == 25


def test_kalman_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        KalmanHedge().filter(np.arange(10.0), np.arange(9.0))
    with pytest.raises(ValueError):
        KalmanHedge(delta=1.5)
    with pytest.raises(ValueError):
        KalmanHedge(r_obs=0.0)
