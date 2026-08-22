from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.analysis.roc import roc_auc, roc_curve


def test_perfect_ranking_auc_is_one() -> None:
    scores = pd.Series([0.1, 0.2, 0.8, 0.9])
    labels = pd.Series([0, 0, 1, 1])
    assert roc_auc(scores, labels) == pytest.approx(1.0)


def test_reversed_ranking_auc_is_zero() -> None:
    scores = pd.Series([0.9, 0.8, 0.2, 0.1])
    labels = pd.Series([0, 0, 1, 1])
    assert roc_auc(scores, labels) == pytest.approx(0.0)


def test_random_auc_near_half() -> None:
    rng = np.random.default_rng(0)
    n = 4000
    scores = pd.Series(rng.uniform(size=n))
    labels = pd.Series(rng.integers(0, 2, size=n))
    assert roc_auc(scores, labels) == pytest.approx(0.5, abs=0.05)


def test_ties_handled_by_average_rank() -> None:
    # All-equal scores → no ranking information → AUC exactly 0.5.
    scores = pd.Series([0.5, 0.5, 0.5, 0.5])
    labels = pd.Series([0, 1, 0, 1])
    assert roc_auc(scores, labels) == pytest.approx(0.5)


def test_single_class_returns_half() -> None:
    assert roc_auc(pd.Series([0.1, 0.9]), pd.Series([1, 1])) == 0.5


def test_roc_curve_endpoints_and_monotonic() -> None:
    scores = pd.Series([0.1, 0.4, 0.35, 0.8])
    labels = pd.Series([0, 0, 1, 1])
    fpr, tpr, _ = roc_curve(scores, labels)
    assert fpr[0] == 0.0 and tpr[0] == 0.0
    assert fpr[-1] == pytest.approx(1.0) and tpr[-1] == pytest.approx(1.0)
    assert np.all(np.diff(fpr) >= -1e-9)  # fpr non-decreasing along the sweep
