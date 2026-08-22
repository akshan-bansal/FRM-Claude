"""Forward-return event labels — the ground truth for signal-quality scoring.

A label at bar ``t`` answers: *did a tradeable up-move actually materialize over
the next ``horizon`` bars starting from t?* This is deliberately forward-looking,
which is legitimate for **evaluation only**. These labels must never be handed to
a Strategy or used as a feature — doing so is lookahead bias. The companion
regression test ``tests/test_labeling.py::test_labels_never_leak_into_features``
guards that boundary.
"""
from __future__ import annotations

import pandas as pd


def forward_return(close: pd.Series, horizon: int) -> pd.Series:
    """Return realized over the next ``horizon`` bars: ``close[t+h]/close[t] - 1``.

    The last ``horizon`` rows have no complete forward window and are returned as
    NaN (unknown outcome), never as 0.
    """
    if horizon < 1:
        raise ValueError(f"horizon must be >= 1; got {horizon}")
    fwd = close.shift(-horizon) / close - 1.0
    return fwd.rename("forward_return")


def label_events(
    df: pd.DataFrame,
    *,
    horizon: int = 10,
    up_threshold: float = 0.03,
) -> pd.Series:
    """Binary ground-truth labels for a *long* opportunity.

    ``label[t] == 1`` when the forward return over the next ``horizon`` bars
    exceeds ``up_threshold`` (a real up-move worth catching), else 0. Bars whose
    forward window runs off the end of the series are labelled ``pd.NA`` (unknown)
    so they are excluded from any confusion matrix rather than counted as
    negatives.

    Returned as a nullable ``Int64`` Series aligned to ``df.index``.
    """
    if "close" not in df.columns:
        raise ValueError("label_events requires a 'close' column")
    fr = forward_return(df["close"], horizon)
    labels = (fr > up_threshold).astype("Int64")
    labels[fr.isna()] = pd.NA
    return labels.rename("label")
