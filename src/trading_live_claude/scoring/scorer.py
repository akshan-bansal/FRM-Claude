"""Scorer — the precision stage that reranks high-recall candidates.

The signal stage (e.g. ``CompositeStrategy``) deliberately over-fires to maximize
recall. The Scorer assigns each candidate a ``score`` in [0, 1] — an estimate of
how likely it is a true positive — and a threshold ``tau`` cuts the weak ones.
Raising ``tau`` trades recall for precision; ``calibrate_threshold`` picks the
``tau`` that maximizes a chosen objective (via the swappable ``ObjectiveAdapter``)
subject to a recall floor.

The default scorer is a **transparent convex blend** of bounded [0, 1] features —
no opaque model — so every score decomposes into named contributions. A learned
model can later implement the same ``score_frame`` seam without changing callers.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..analysis.classification import ClassificationReport, confusion
from ..signals.generator import candidate_strength
from ..signals.indicators import sma
from .objective import ObjectiveAdapter, ObjectiveInput


def _trend_alignment(df: pd.DataFrame, window: int = 200) -> pd.Series:
    """Soft [0, 1] long-regime feature: how far price sits above its slow SMA.

    Mapped through a bounded squash so it never dominates the blend. NaN warm-up
    (and the no-close case) collapses to a neutral 0.5.
    """
    if "close" not in df.columns:
        return pd.Series(0.5, index=df.index)
    slow = sma(df["close"], window)
    rel = (df["close"] - slow) / slow
    aligned = 0.5 + 0.5 * np.tanh(rel * 10.0)
    return aligned.fillna(0.5).clip(0.0, 1.0)


@dataclass
class ScorerConfig:
    """Named feature weights for the convex blend. Weights are re-normalized, so
    only their *ratios* matter. Default leans entirely on candidate strength
    (detector agreement); add ``trend_alignment`` to fold in regime context."""

    weights: dict[str, float] = field(default_factory=lambda: {"signal_strength": 1.0})


class Scorer:
    """Rerank candidates into a precision-oriented ``score`` in [0, 1]."""

    def __init__(self, config: ScorerConfig | None = None) -> None:
        self.config = config or ScorerConfig()
        total = sum(self.config.weights.values())
        if total <= 0:
            raise ValueError("Scorer weights must sum to a positive value")
        self._norm = {k: v / total for k, v in self.config.weights.items()}

    def _features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Assemble the bounded [0, 1] feature columns the config asks for."""
        feats: dict[str, pd.Series] = {}
        for name in self._norm:
            if name == "signal_strength":
                feats[name] = candidate_strength(df)
            elif name == "trend_alignment":
                feats[name] = _trend_alignment(df)
            elif name in df.columns:
                feats[name] = df[name].astype(float).fillna(0.0).clip(0.0, 1.0)
            else:
                raise KeyError(
                    f"Scorer feature {name!r} not available (no column and no builtin)"
                )
        return pd.DataFrame(feats, index=df.index)

    def score_frame(self, df: pd.DataFrame) -> pd.Series:
        """Convex-blend the features into a single ``score`` Series in [0, 1]."""
        feats = self._features(df)
        score = pd.Series(0.0, index=df.index)
        for name, w in self._norm.items():
            score = score + w * feats[name]
        return score.clip(0.0, 1.0).rename("score")

    def gate(self, df: pd.DataFrame, tau: float) -> pd.Series:
        """Boolean entry gate: a candidate survives when it fired AND scores >= tau.

        This sits upstream of, and never relaxes, the Router's risk gates.
        """
        entry = df["entry"].fillna(0).astype(int).astype(bool)
        return (entry & (self.score_frame(df) >= tau)).rename("gated_entry")

    def report_at(
        self, df: pd.DataFrame, labels: pd.Series, tau: float
    ) -> ClassificationReport:
        """Confusion report for the gated entries at threshold ``tau``."""
        return confusion(self.gate(df, tau).astype(int), labels)


@dataclass(frozen=True)
class ThresholdChoice:
    tau: float
    objective_value: float
    precision: float
    recall: float


def calibrate_threshold(
    scorer: Scorer,
    df: pd.DataFrame,
    labels: pd.Series,
    *,
    objective: str = "precision_at_recall",
    min_recall: float = 0.0,
    grid: int = 21,
) -> ThresholdChoice:
    """Sweep ``tau`` over [0, 1] and pick the threshold maximizing ``objective``.

    Uses the swappable ``ObjectiveAdapter``, so the calibration target is a config
    string. Candidates failing ``min_recall`` are skipped; if none qualify, the
    lowest threshold (most permissive) is returned so the pipeline still trades.
    """
    adapter = ObjectiveAdapter.from_name(objective)
    taus = np.linspace(0.0, 1.0, grid)
    best: ThresholdChoice | None = None
    fallback: ThresholdChoice | None = None

    for tau in taus:
        rep = scorer.report_at(df, labels, float(tau))
        val = adapter.score(
            ObjectiveInput(
                sharpe=0.0,
                max_drawdown=0.0,
                precision=rep.precision,
                recall=rep.recall,
            )
        )
        choice = ThresholdChoice(float(tau), val, rep.precision, rep.recall)
        if fallback is None or tau < fallback.tau:
            fallback = choice
        if rep.recall < min_recall:
            continue
        if best is None or val > best.objective_value:
            best = choice

    assert fallback is not None  # grid is non-empty
    return best or fallback
