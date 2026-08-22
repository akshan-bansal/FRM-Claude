from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.analysis.classification import confusion
from trading_live_claude.scoring.scorer import (
    Scorer,
    ScorerConfig,
    calibrate_threshold,
)


def _candidates_with_informative_strength(n: int = 400, seed: int = 7) -> tuple[pd.DataFrame, pd.Series]:
    """Build candidates whose signal_strength genuinely correlates with the label.

    Every row is a fired entry. The label is 1 with probability equal to the row's
    strength, so a strength threshold MUST raise precision. This tests the precision
    stage's mechanism deterministically, independent of any market assumption.
    """
    rng = np.random.default_rng(seed)
    strength = rng.uniform(0.0, 1.0, n)
    labels = (rng.uniform(0.0, 1.0, n) < strength).astype(int)
    df = pd.DataFrame(
        {
            "close": np.full(n, 100.0),
            "entry": np.ones(n, dtype=int),
            "signal_strength": strength,
        }
    )
    return df, pd.Series(labels)


def test_score_frame_bounded_and_monotone_in_strength() -> None:
    df = pd.DataFrame(
        {"entry": [1, 1, 1], "signal_strength": [0.1, 0.5, 0.9], "close": [100, 100, 100]}
    )
    score = Scorer().score_frame(df)
    assert (score >= 0.0).all() and (score <= 1.0).all()
    assert score.iloc[0] < score.iloc[1] < score.iloc[2]


def test_thresholding_raises_precision() -> None:
    """The core precision-stage claim: gating on score >= tau beats un-gated."""
    df, labels = _candidates_with_informative_strength()
    scorer = Scorer()

    baseline = confusion(df["entry"], labels).precision
    gated = scorer.report_at(df, labels, tau=0.7).precision

    assert gated > baseline


def test_gate_never_exceeds_raw_entries() -> None:
    df, _labels = _candidates_with_informative_strength()
    scorer = Scorer()
    raw = int(df["entry"].sum())
    gated = int(scorer.gate(df, tau=0.5).sum())
    assert gated <= raw  # a filter only ever removes candidates


def test_calibrate_threshold_respects_recall_floor() -> None:
    df, labels = _candidates_with_informative_strength()
    scorer = Scorer()
    choice = calibrate_threshold(
        scorer, df, labels, objective="precision_at_recall", min_recall=0.5
    )
    assert 0.0 <= choice.tau <= 1.0
    assert choice.recall >= 0.5 - 1e-9


def test_calibrate_falls_back_when_floor_unreachable() -> None:
    df, labels = _candidates_with_informative_strength()
    scorer = Scorer()
    # No threshold can achieve recall 2.0 — must fall back, not crash.
    choice = calibrate_threshold(scorer, df, labels, min_recall=2.0)
    assert choice.tau == pytest.approx(0.0)


def test_trend_alignment_feature_available_without_column() -> None:
    n = 300
    close = np.linspace(100, 200, n)  # clean uptrend
    df = pd.DataFrame({"close": close, "entry": np.ones(n, dtype=int)})
    scorer = Scorer(ScorerConfig(weights={"trend_alignment": 1.0}))
    score = scorer.score_frame(df)
    # Late in a strong uptrend, alignment (and thus score) should be high.
    assert score.iloc[-1] > 0.6


def test_zero_weights_rejected() -> None:
    with pytest.raises(ValueError):
        Scorer(ScorerConfig(weights={"signal_strength": 0.0}))


def test_unknown_feature_rejected() -> None:
    df = pd.DataFrame({"entry": [1], "close": [100.0]})
    with pytest.raises(KeyError):
        Scorer(ScorerConfig(weights={"nonexistent_feature": 1.0})).score_frame(df)
