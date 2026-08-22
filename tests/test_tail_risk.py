from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.risk.tail import (
    conditional_drawdown_at_risk,
    cornish_fisher_var,
    downside_deviation,
    expected_shortfall,
    loss_probability,
    omega_ratio,
    tail_ratio,
    ulcer_index,
    value_at_risk,
)


def test_var_and_es_positive_and_ordered() -> None:
    # A known left tail: mostly small, a few large losses.
    r = pd.Series([-0.10, -0.08, -0.05, 0.0, 0.01, 0.01, 0.02, 0.02, 0.03, 0.03])
    var = value_at_risk(r, alpha=0.2)
    es = expected_shortfall(r, alpha=0.2)
    assert var > 0 and es > 0
    assert es >= var  # ES averages the tail, so it is at least as large as VaR


def test_expected_shortfall_is_tail_mean() -> None:
    r = pd.Series([-0.20, -0.10] + [0.01] * 8)  # worst 20% = the two big losses
    es = expected_shortfall(r, alpha=0.2)
    assert es == pytest.approx(0.15, abs=1e-9)  # mean(0.20, 0.10)


def test_cornish_fisher_exceeds_gaussian_on_fat_left_tail() -> None:
    rng = np.random.default_rng(0)
    normal = pd.Series(rng.normal(0, 0.01, 5000))
    fat = normal.copy()
    fat.iloc[:50] = -0.15  # inject a heavy, skewed left tail
    assert cornish_fisher_var(fat) > cornish_fisher_var(normal)


def test_loss_probability_and_omega() -> None:
    r = pd.Series([-0.02, -0.01, 0.0, 0.01, 0.03])
    assert loss_probability(r) == pytest.approx(0.4)  # 2 of 5 below 0
    assert omega_ratio(r) > 0.0


def test_downside_deviation_only_counts_losses() -> None:
    r = pd.Series([0.05, 0.05, -0.05])
    assert downside_deviation(r) == pytest.approx(0.05 / np.sqrt(3), rel=1e-6)


def test_ulcer_and_cdar_positive_under_drawdown() -> None:
    r = pd.Series([-0.1, -0.1, -0.1, 0.02, 0.02])  # sustained drawdown
    assert ulcer_index(r) > 0
    assert conditional_drawdown_at_risk(r, alpha=0.5) > 0


def test_tail_ratio_symmetry() -> None:
    sym = pd.Series(np.concatenate([np.linspace(-0.05, 0.05, 100)]))
    assert tail_ratio(sym) == pytest.approx(1.0, abs=0.15)


def test_empty_series_safe() -> None:
    empty = pd.Series(dtype=float)
    assert value_at_risk(empty) == 0.0
    assert expected_shortfall(empty) == 0.0
    assert cornish_fisher_var(empty) == 0.0
    assert ulcer_index(empty) == 0.0
