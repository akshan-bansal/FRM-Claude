"""ROC / AUC for graded signals against forward-return labels.

Point metrics (sensitivity, specificity at one threshold) describe a single
operating point. ROC sweeps the *graded* signal strength across every threshold and
plots the true-positive rate against the false-positive rate; the area under that
curve (AUC) is a threshold-independent measure of how well the signal *ranks* real
moves above non-moves. 0.5 = coin flip, 1.0 = perfect ranking.

``roc_auc`` uses the rank-based (Mann-Whitney) identity so it handles the many tied
scores that binary/coarse signals produce. ``roc_curve`` returns the sweep for
plotting (including the 3-D sensitivity/specificity/threshold view).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _clean(scores: pd.Series, labels: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    joined = pd.DataFrame({"s": scores, "y": labels}).dropna()
    s = joined["s"].astype(float).to_numpy()
    y = joined["y"].astype(float).round().astype(int).to_numpy()
    return s, y


def roc_auc(scores: pd.Series, labels: pd.Series) -> float:
    """Area under the ROC curve via average-rank Mann-Whitney. 0.5 when undefined."""
    s, y = _clean(scores, labels)
    n_pos = int((y == 1).sum())
    n_neg = int((y == 0).sum())
    if n_pos == 0 or n_neg == 0:
        return 0.5  # AUC undefined with only one class present
    ranks = pd.Series(s).rank(method="average").to_numpy()
    auc = (ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
    return float(auc)


def roc_curve(scores: pd.Series, labels: pd.Series) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (fpr, tpr, thresholds) sweeping the score high→low.

    Thresholds are the unique score values (descending); each yields one (fpr, tpr)
    point. The curve starts at (0,0) and ends at (1,1).
    """
    s, y = _clean(scores, labels)
    n_pos = int((y == 1).sum())
    n_neg = int((y == 0).sum())
    if n_pos == 0 or n_neg == 0:
        return np.array([0.0, 1.0]), np.array([0.0, 1.0]), np.array([1.0, 0.0])

    thresholds = np.unique(s)[::-1]  # high → low
    fpr = [0.0]
    tpr = [0.0]
    for t in thresholds:
        fired = s >= t
        tp = int(((y == 1) & fired).sum())
        fp = int(((y == 0) & fired).sum())
        tpr.append(tp / n_pos)
        fpr.append(fp / n_neg)
    return np.array(fpr), np.array(tpr), np.concatenate([[np.inf], thresholds])
