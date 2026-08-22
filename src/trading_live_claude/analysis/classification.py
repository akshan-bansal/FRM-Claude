"""Confusion-matrix scoring of a 0/1 signal against forward-return labels.

``recall`` measures the signal-processing stage (did we catch the real moves?),
``precision`` measures the scoring/decision stage (of the ones we fired on, how
many were real?). ``specificity`` and ``f_beta`` round out the picture. All
divisions are guarded so an empty or degenerate confusion matrix returns 0.0
rather than raising.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


def _safe_div(num: float, den: float) -> float:
    return float(num) / float(den) if den else 0.0


@dataclass(frozen=True)
class ClassificationReport:
    """Confusion matrix plus the standard derived rates."""

    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def support(self) -> int:
        """Total labelled samples the report was computed over."""
        return self.tp + self.fp + self.fn + self.tn

    @property
    def precision(self) -> float:
        """TP / (TP + FP) — quality of the signals we act on. Scoring-stage metric."""
        return _safe_div(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float:
        """TP / (TP + FN) — fraction of real moves caught. Signal-stage metric."""
        return _safe_div(self.tp, self.tp + self.fn)

    @property
    def specificity(self) -> float:
        """TN / (TN + FP) — fraction of non-moves correctly avoided."""
        return _safe_div(self.tn, self.tn + self.fp)

    @property
    def f1(self) -> float:
        return self.f_beta(1.0)

    def f_beta(self, beta: float) -> float:
        """F-beta. beta < 1 favours precision; beta > 1 favours recall."""
        p, r = self.precision, self.recall
        b2 = beta * beta
        return _safe_div((1.0 + b2) * p * r, b2 * p + r)

    def as_dict(self) -> dict[str, float | int]:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "support": self.support,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "specificity": round(self.specificity, 4),
            "f1": round(self.f1, 4),
        }


def confusion(
    signal: pd.Series,
    labels: pd.Series,
    *,
    execution_lag: int = 0,
) -> ClassificationReport:
    """Build a confusion matrix aligning a 0/1 ``signal`` against 0/1 ``labels``.

    Rows where either side is NaN/NA (signal warm-up, or the trailing bars with no
    forward window) are dropped before counting. ``execution_lag`` shifts the
    signal forward by N bars before comparison, to judge the position actually
    taken under a trade-on-next-bar convention (default 0 = evaluate the decision
    at the bar it was made).
    """
    sig = signal.shift(execution_lag) if execution_lag else signal
    joined = pd.DataFrame({"sig": sig, "lab": labels}).dropna()
    if joined.empty:
        return ClassificationReport(0, 0, 0, 0)

    s = joined["sig"].astype(float).clip(0, 1).round().astype(bool)
    lab = joined["lab"].astype(float).round().astype(bool)

    tp = int((s & lab).sum())
    fp = int((s & ~lab).sum())
    fn = int((~s & lab).sum())
    tn = int((~s & ~lab).sum())
    return ClassificationReport(tp=tp, fp=fp, fn=fn, tn=tn)
