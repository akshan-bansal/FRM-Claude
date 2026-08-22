from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.analysis.fidelity import fidelity, fidelity_consistency


def test_persistent_positive_edge_high_fidelity() -> None:
    rng = np.random.default_rng(0)
    n = 500
    signal = pd.Series(rng.uniform(size=n))
    fwd = signal * 0.1 + rng.normal(0, 0.01, n)  # forward return tracks the signal
    assert fidelity(signal, fwd) > 0.7
    assert fidelity_consistency(signal, fwd) > 0.9


def test_inverted_edge_negative_fidelity() -> None:
    rng = np.random.default_rng(1)
    n = 500
    signal = pd.Series(rng.uniform(size=n))
    fwd = -signal * 0.1 + rng.normal(0, 0.01, n)  # relationship inverted
    assert fidelity(signal, fwd) < -0.5


def test_no_relationship_near_zero() -> None:
    rng = np.random.default_rng(2)
    n = 2000
    signal = pd.Series(rng.uniform(size=n))
    fwd = pd.Series(rng.normal(0, 0.02, n))  # independent
    assert fidelity(signal, fwd) == pytest.approx(0.0, abs=0.1)


def test_insufficient_data_returns_zero() -> None:
    assert fidelity(pd.Series([0.5, 0.6]), pd.Series([0.01, 0.02]), window=63) == 0.0


def test_constant_signal_is_finite_zero() -> None:
    # A constant signal has zero variance → correlation is undefined (NaN/inf), which
    # must collapse to a finite 0.0, never leak inf into downstream scoring.
    n = 300
    const = pd.Series(np.ones(n))
    fwd = pd.Series(np.random.default_rng(3).normal(0, 0.02, n))
    result = fidelity(const, fwd)
    assert np.isfinite(result)
    assert result == 0.0
