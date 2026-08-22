from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.analysis.classification import ClassificationReport, confusion


def test_confusion_counts_known_matrix() -> None:
    #        label:  1  0  1  0  1
    signal = pd.Series([1, 1, 0, 0, 1])
    labels = pd.Series([1, 0, 1, 0, 1])
    r = confusion(signal, labels)
    assert (r.tp, r.fp, r.fn, r.tn) == (2, 1, 1, 1)
    assert r.support == 5


def test_derived_rates() -> None:
    r = ClassificationReport(tp=2, fp=1, fn=1, tn=1)
    assert r.precision == pytest.approx(2 / 3)
    assert r.recall == pytest.approx(2 / 3)
    assert r.specificity == pytest.approx(1 / 2)
    assert r.f1 == pytest.approx(2 / 3)


def test_f_beta_precision_vs_recall_weighting() -> None:
    # High recall, low precision: beta<1 (precision-leaning) should score below
    # beta>1 (recall-leaning).
    r = ClassificationReport(tp=9, fp=9, fn=1, tn=1)  # recall .9, precision .5
    assert r.f_beta(0.5) < r.f_beta(2.0)


def test_empty_and_degenerate_are_zero_not_error() -> None:
    empty = confusion(pd.Series([np.nan, np.nan]), pd.Series([np.nan, np.nan]))
    assert empty.support == 0
    assert empty.precision == 0.0 and empty.recall == 0.0


def test_na_rows_are_dropped() -> None:
    # Trailing NA label (unknown outcome) must not be counted as a negative.
    signal = pd.Series([1, 1, 1], dtype="float")
    labels = pd.Series([1, 0, pd.NA], dtype="Int64")
    r = confusion(signal, labels)
    assert r.support == 2
    assert (r.tp, r.fp) == (1, 1)


def test_execution_lag_shifts_signal() -> None:
    # Signal fires one bar before the labelled move; lag=1 aligns them.
    signal = pd.Series([1, 0, 0])
    labels = pd.Series([0, 1, 0])
    assert confusion(signal, labels, execution_lag=0).tp == 0
    assert confusion(signal, labels, execution_lag=1).tp == 1
