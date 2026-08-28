from __future__ import annotations

import numpy as np
import pandas as pd

from trading_live_claude.models.timeseries import (
    ma_ladder,
    rolling_arima_forecast,
    rolling_garch_vol,
)


def _returns(n: int = 200, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(0.0003, 0.01, n)


def test_ma_ladder_bounded_and_causal() -> None:
    rng = np.random.default_rng(2)
    price = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.001, 0.01, 300))))
    score = ma_ladder(price, windows=(5, 10, 20))
    assert score.between(-1.0, 1.0).all()
    # Shifted by one bar: the score at t must not depend on close[t].
    bumped = price.copy()
    bumped.iloc[150] *= 1.5
    s2 = ma_ladder(bumped, windows=(5, 10, 20))
    assert score.iloc[150] == s2.iloc[150]  # same-bar change does not move the current score


def test_rolling_arima_forecast_shape_warmup_causal() -> None:
    r = _returns(220)
    fc = rolling_arima_forecast(r, order=(1, 0, 0), window=80)
    assert fc.shape == (220,)
    assert np.isnan(fc[:80]).all()          # warm-up
    assert np.isfinite(fc[80:]).all()
    part = rolling_arima_forecast(r[:150], order=(1, 0, 0), window=80)
    assert np.allclose(fc[80:120], part[80:120], atol=1e-8, equal_nan=True)  # causal prefix


def test_rolling_garch_vol_positive_after_warmup() -> None:
    r = _returns(220, seed=4)
    vol = rolling_garch_vol(r, window=80)
    assert vol.shape == (220,)
    assert np.isnan(vol[:80]).all()
    tail = vol[80:]
    assert np.isfinite(tail).all() and (tail > 0).all()
