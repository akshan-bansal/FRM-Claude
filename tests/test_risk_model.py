from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.config.settings import Settings
from trading_live_claude.risk.risk_model import per_trade_risk, portfolio_risk


def test_shipped_defaults_are_tail_and_correlation_aware() -> None:
    s = Settings()
    assert s.risk_model == "cvar"        # tail-aware per-trade risk
    assert s.heat_aggregation == "corr"  # covariance-aware heat


def _rets(n: int = 120, seed: int = 0, scale: float = 0.02) -> pd.Series:
    return pd.Series(np.random.default_rng(seed).normal(0.0, scale, n))


# ---- per-trade risk (#1) --------------------------------------------------------

def test_atr_model_is_shares_times_stop_distance() -> None:
    assert per_trade_risk(100, 50.0, stop_distance=2.0, model="atr") == 200.0


def test_cvar_is_at_least_as_large_as_var() -> None:
    r = _rets(seed=1)
    var = per_trade_risk(100, 50.0, returns=r, model="var")
    cvar = per_trade_risk(100, 50.0, returns=r, model="cvar")
    assert cvar >= var > 0.0  # Expected Shortfall is the mean beyond VaR


def test_tail_models_fall_back_to_atr_without_history() -> None:
    assert per_trade_risk(100, 50.0, stop_distance=2.0, returns=None, model="cvar") == 200.0
    assert per_trade_risk(100, 50.0, stop_distance=2.0, returns=_rets(n=5), model="cvar") == 200.0


def test_zero_when_no_basis() -> None:
    assert per_trade_risk(100, 50.0, model="atr") == 0.0  # no stop distance, no returns


# ---- portfolio aggregation (#2) -------------------------------------------------

def test_sum_is_correlation_blind_total() -> None:
    risks = {"A": 100.0, "B": 100.0}
    assert portfolio_risk(risks, {"A": None, "B": None}, method="sum") == 200.0


def test_perfectly_correlated_equals_the_sum() -> None:
    r = _rets(seed=2)
    risks = {"A": 100.0, "B": 100.0}
    combined = portfolio_risk(risks, {"A": r, "B": r.copy()}, method="corr")
    assert combined == pytest.approx(200.0, abs=1e-6)


def test_diversification_credits_below_the_sum() -> None:
    risks = {"A": 100.0, "B": 100.0}
    combined = portfolio_risk(risks, {"A": _rets(seed=3), "B": _rets(seed=4)}, method="corr")
    assert 100.0 < combined < 200.0  # two ~uncorrelated names ~ sqrt(2)*100 ≈ 141


def test_corr_falls_back_to_sum_without_history() -> None:
    risks = {"A": 100.0, "B": 100.0}
    assert portfolio_risk(risks, {"A": None, "B": None}, method="corr") == 200.0


def test_names_without_history_added_standalone() -> None:
    r = _rets(seed=5)
    # A and B correlate; C has no history → added on top at its standalone risk.
    risks = {"A": 100.0, "B": 100.0, "C": 50.0}
    combined = portfolio_risk(risks, {"A": r, "B": r.copy(), "C": None}, method="corr")
    assert combined == pytest.approx(250.0, abs=1e-6)

