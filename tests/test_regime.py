from __future__ import annotations

import numpy as np
import pandas as pd

from trading_live_claude.models import RegimeClassifier


def _uptrend(n: int = 400, seed: int = 1) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0012, 0.008, n))))  # steady low-vol rise


def _crash_tail(n: int = 400, seed: int = 2) -> pd.Series:
    rng = np.random.default_rng(seed)
    up = np.cumsum(rng.normal(0.0012, 0.008, n - 60))
    down = up[-1] + np.cumsum(rng.normal(-0.01, 0.035, 60))  # sharp high-vol selloff
    return pd.Series(100 * np.exp(np.concatenate([up, down])))


def test_risk_scalar_bounded() -> None:
    clf = RegimeClassifier(floor=0.2)
    s = clf.series(_uptrend())
    assert s["risk_scalar"].between(0.2, 1.0).all()


def test_calm_uptrend_is_risk_on() -> None:
    st = RegimeClassifier().classify(_uptrend())
    assert st.risk_scalar >= 0.75 and st.label == "risk_on"
    assert st.trend > 0 and st.drawdown > -0.05


def test_crash_stands_the_book_down() -> None:
    st = RegimeClassifier().classify(_crash_tail())
    assert st.risk_scalar < 0.6            # gates ramp exposure down in the selloff
    assert st.label in {"neutral", "risk_off"}
    assert st.drawdown < -0.05


def test_regime_is_causal() -> None:
    """The scalar at each bar uses only prior data — truncating must not change earlier values."""
    p = _crash_tail()
    full = RegimeClassifier().series(p)["risk_scalar"].to_numpy()
    part = RegimeClassifier().series(p.iloc[:300])["risk_scalar"].to_numpy()
    assert np.allclose(full[:280], part[:280], atol=1e-9, equal_nan=True)
