"""Signal-quality analysis: forward-return labels + precision/recall measurement.

This package is the *ruler* for the two-stage recall->precision pipeline. It does
NOT influence trading decisions: labels use forward returns and therefore must
never be fed back into a Strategy (that would be lookahead). They exist purely to
score how well a strategy's signals classify real opportunities.
"""
from __future__ import annotations

from .classification import ClassificationReport, confusion
from .fidelity import fidelity, fidelity_consistency, rolling_correlation
from .labeling import forward_return, label_events
from .matrix import MatrixCell, build_signal_matrix, render_matrix_markdown
from .roc import roc_auc, roc_curve
from .universe import (
    SEED_UNIVERSE,
    UniverseFilter,
    UniverseMember,
    screen_universe,
    seed_symbols,
    select_universe,
)

__all__ = [
    "SEED_UNIVERSE",
    "ClassificationReport",
    "MatrixCell",
    "UniverseFilter",
    "UniverseMember",
    "build_signal_matrix",
    "confusion",
    "fidelity",
    "fidelity_consistency",
    "forward_return",
    "label_events",
    "render_matrix_markdown",
    "roc_auc",
    "roc_curve",
    "rolling_correlation",
    "screen_universe",
    "seed_symbols",
    "select_universe",
]
