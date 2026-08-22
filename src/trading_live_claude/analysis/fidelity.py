"""Fidelity — the temporal stability of a signal's edge.

The point metrics (sensitivity/specificity/precision) and AUC all collapse the whole
history into one number, hiding *when* the edge existed. A signal that predicted
beautifully for two years and then inverted can score the same as one that was
mildly, consistently right throughout. Fidelity separates them: it is the **mean of
the rolling correlation between the graded signal and the realized forward return**,
so it rewards an edge that persists faithfully over time and penalizes one that
drifts or flips.

Range is [-1, 1] (a correlation). ~0 = no persistent relationship; negative = the
signal's relationship to forward returns inverted over the window.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def rolling_correlation(
    signal: pd.Series, forward_return: pd.Series, *, window: int = 63, min_periods: int = 20
) -> pd.Series:
    """Rolling Pearson correlation between signal and forward return over ``window`` bars.

    Windows with zero variance (a constant signal) yield NaN/±inf from the correlation;
    those are dropped so only well-defined correlations remain.
    """
    joined = pd.DataFrame({"s": signal, "r": forward_return}).dropna()
    if joined.empty:
        return pd.Series(dtype=float)
    rc = joined["s"].rolling(window, min_periods=min_periods).corr(joined["r"])
    return rc.replace([np.inf, -np.inf], np.nan).dropna()


def fidelity(
    signal: pd.Series, forward_return: pd.Series, *, window: int = 63, min_periods: int = 20
) -> float:
    """Mean rolling signal↔forward-return correlation (temporal edge fidelity).

    0.0 when there is not enough data to form a single window.
    """
    rc = rolling_correlation(signal, forward_return, window=window, min_periods=min_periods)
    if not len(rc):
        return 0.0
    return float(np.clip(rc.mean(), -1.0, 1.0))  # a correlation is bounded to [-1, 1]


def fidelity_consistency(
    signal: pd.Series, forward_return: pd.Series, *, window: int = 63, min_periods: int = 20
) -> float:
    """Fraction of rolling windows in which the correlation stayed positive ([0, 1])."""
    rc = rolling_correlation(signal, forward_return, window=window, min_periods=min_periods)
    return float((rc > 0).mean()) if len(rc) else 0.0
