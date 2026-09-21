"""Paired effect size / HAC t-stat."""
from __future__ import annotations

import numpy as np
import pytest

from trading_live_claude.analysis.effect_size import newey_west_lags, paired_effect


def test_iid_hac_matches_plain_t() -> None:
    rng = np.random.default_rng(0)
    d = 0.1 + rng.standard_normal(5000)
    e = paired_effect(d, lags=0)
    assert e.hac_se == pytest.approx(d.std(ddof=0) / np.sqrt(len(d)))
    assert e.p_value < 1e-10
    assert e.cohens_dz == pytest.approx(0.1, abs=0.03)


def test_autocorrelation_widens_se() -> None:
    rng = np.random.default_rng(1)
    eps = rng.standard_normal(3000)
    d = np.convolve(eps, np.ones(10) / 10, mode="valid")   # MA(9): strongly autocorrelated
    assert paired_effect(d).hac_se > paired_effect(d, lags=0).hac_se * 1.5


def test_null_is_not_significant() -> None:
    d = np.random.default_rng(2).standard_normal(1000)
    assert paired_effect(d).p_value > 0.01
    assert newey_west_lags(1000) == 6
