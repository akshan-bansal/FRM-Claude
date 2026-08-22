"""Swappable objective functions for tuning and candidate scoring.

An *objective* maps one backtest/classification result to a single scalar that the
tuner maximizes. Objectives are registered by name in ``OBJECTIVES`` and selected
through ``ObjectiveAdapter.from_name(...)`` (or ``get_objective(...)``), so the
optimization target is a swappable config string rather than hardcoded logic.

The pipeline's division of labour maps directly onto these:
  * signal stage  -> optimize ``recall``   (catch every real move)
  * scoring stage -> optimize ``precision`` / ``f_beta`` (act only on good ones)

Parameterized objectives (``f_beta``, ``precision_at_recall``) are exposed both as
factories (for custom params) and pre-registered under canonical names with sane
defaults. Any precision/recall-based objective returns ``MISSING_PENALTY`` when the
result carries no classification metrics, so an unmeasured run can never win.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..backtest.metrics import Metrics

# Returned by classification-based objectives when labels were never supplied, so
# an unmeasured run sorts to the bottom instead of silently scoring 0.
MISSING_PENALTY: float = -1e9


@dataclass(frozen=True)
class ObjectiveInput:
    """Everything an objective may consult to score a single (strategy, symbol) run.

    P&L fields are always present; classification fields are ``None`` until the
    measurement harness supplies forward-return labels.
    """

    sharpe: float
    max_drawdown: float
    cagr: float = 0.0
    win_rate: float = 0.0
    num_trades: int = 0
    precision: float | None = None
    recall: float | None = None
    specificity: float | None = None
    f1: float | None = None
    roc_auc: float | None = None
    fidelity: float | None = None
    cvar: float | None = None  # Expected Shortfall (positive loss magnitude)

    @classmethod
    def from_metrics(cls, m: Metrics) -> ObjectiveInput:
        return cls(
            sharpe=m.sharpe,
            max_drawdown=m.max_drawdown,
            cagr=m.cagr,
            win_rate=m.win_rate,
            num_trades=m.num_trades,
            precision=m.precision,
            recall=m.recall,
            specificity=m.specificity,
            f1=m.f1,
        )


# An objective is any callable turning an ObjectiveInput into a scalar to maximize.
ObjectiveFn = Callable[[ObjectiveInput], float]

_REGISTRY: dict[str, ObjectiveFn] = {}


def register_objective(name: str, fn: ObjectiveFn) -> ObjectiveFn:
    """Register ``fn`` under ``name`` (idempotent overwrite). Returns ``fn``."""
    _REGISTRY[name] = fn
    return fn


def get_objective(name: str) -> ObjectiveFn:
    """Look up a registered objective, with a helpful error listing the options."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"Unknown objective {name!r}. Registered: {sorted(_REGISTRY)}"
        ) from None


# Read-only view for callers that want to enumerate available objectives.
OBJECTIVES: dict[str, ObjectiveFn] = _REGISTRY


class ObjectiveAdapter:
    """Runtime-swappable wrapper around a named objective.

    Use ``ObjectiveAdapter.from_name("precision")`` to select the target, then call
    ``.score(objective_input)`` wherever the tuner needs a scalar. Swapping the
    optimization target is a one-line change of the name string.
    """

    def __init__(self, fn: ObjectiveFn, name: str) -> None:
        self.fn = fn
        self.name = name

    @classmethod
    def from_name(cls, name: str) -> ObjectiveAdapter:
        return cls(get_objective(name), name)

    def score(self, x: ObjectiveInput) -> float:
        return self.fn(x)

    def __repr__(self) -> str:
        return f"ObjectiveAdapter(name={self.name!r})"


# --------------------------------------------------------------------------- #
# Concrete objectives
# --------------------------------------------------------------------------- #


def sharpe_over_dd(x: ObjectiveInput) -> float:
    """Legacy P&L objective: Sharpe / |max drawdown|, drawdown floored to avoid /0.

    Ported verbatim from ``tune.TuneResult.from_backtest`` so switching the default
    away from — and back to — this objective is behaviour-preserving.
    """
    dd = abs(x.max_drawdown) if x.max_drawdown else 0.0
    return x.sharpe / max(dd, 1e-4)


def precision_objective(x: ObjectiveInput) -> float:
    """Maximize precision — the scoring stage's target."""
    return x.precision if x.precision is not None else MISSING_PENALTY


def recall_objective(x: ObjectiveInput) -> float:
    """Maximize recall — the signal stage's target."""
    return x.recall if x.recall is not None else MISSING_PENALTY


def make_f_beta(beta: float) -> ObjectiveFn:
    """Factory: F-beta objective. ``beta < 1`` favours precision, ``> 1`` recall."""
    b2 = beta * beta

    def _f_beta(x: ObjectiveInput) -> float:
        if x.precision is None or x.recall is None:
            return MISSING_PENALTY
        p, r = x.precision, x.recall
        denom = b2 * p + r
        return (1.0 + b2) * p * r / denom if denom else 0.0

    return _f_beta


def make_precision_at_recall(recall_floor: float) -> ObjectiveFn:
    """Factory: maximize precision, but hard-penalize runs under a recall floor.

    Encodes the pipeline's contract directly: pick the highest-precision config
    that still catches enough of the real moves (recall >= floor).
    """

    def _par(x: ObjectiveInput) -> float:
        if x.precision is None or x.recall is None:
            return MISSING_PENALTY
        if x.recall < recall_floor:
            # Below the floor: rank by how far short, always worse than any pass.
            return MISSING_PENALTY + x.recall
        return x.precision

    return _par


# Default weights for the dot-product objective, over the metric vector
# [sensitivity, specificity, precision, risk]. Weights sum to 1 so — since every
# vector component lands in [0, 1] — the resulting score is itself in [0, 1].
DEFAULT_METRIC_WEIGHTS: dict[str, float] = {
    "sensitivity": 0.20,
    "specificity": 0.20,
    "precision": 0.25,
    "risk": 0.15,
    "fidelity": 0.20,
}


def make_dot_product(
    weights: dict[str, float] | None = None, *, risk_source: str = "drawdown"
) -> ObjectiveFn:
    """Weighted dot product of the 5-D metric vector.

    Vector = [sensitivity, specificity, precision, risk, fidelity], each mapped into
    [0, 1] before the dot product: sensitivity=recall, specificity/precision as-is,
    and fidelity = ``max(0, mean rolling correlation)``. The **risk** axis is chosen by
    ``risk_source``: ``"drawdown"`` uses ``1 + max_drawdown`` (shallower is better);
    ``"cvar"`` uses ``1 - min(1, CVaR)`` so a thinner tail (smaller Expected Shortfall)
    scores higher — the tail-risk-aware variant. Returns ``MISSING_PENALTY`` when
    unmeasured.
    """
    w = dict(DEFAULT_METRIC_WEIGHTS if weights is None else weights)

    def _dot(x: ObjectiveInput) -> float:
        if x.recall is None or x.precision is None:
            return MISSING_PENALTY
        spec = x.specificity if x.specificity is not None else 0.0
        fid = max(0.0, x.fidelity) if x.fidelity is not None else 0.0
        if risk_source == "cvar":
            cvar = x.cvar if x.cvar is not None else 0.0
            risk = 1.0 - min(1.0, max(0.0, cvar))
        else:
            risk = 1.0 + x.max_drawdown
        vector = {
            "sensitivity": x.recall,
            "specificity": spec,
            "precision": x.precision,
            "risk": risk,
            "fidelity": fid,
        }
        return sum(w.get(name, 0.0) * value for name, value in vector.items())

    return _dot


def expected_value(x: ObjectiveInput) -> float:
    """Precision-driven expected value per signal at the framework's 2:1 target:stop.

    With a 2R target and 1R stop (``PositionSizer`` defaults), EV in R units is
    ``precision * 2 - (1 - precision) * 1 = 3 * precision - 1``. Positive means a
    positive-expectancy signal at that risk/reward.
    """
    if x.precision is None:
        return MISSING_PENALTY
    return 3.0 * x.precision - 1.0


# Pre-register canonical names. Defaults chosen precision-first: ``f_beta`` uses
# beta=0.5 (weights precision above recall), matching the scoring stage's job.
register_objective("sharpe_over_dd", sharpe_over_dd)
register_objective("precision", precision_objective)
register_objective("recall", recall_objective)
register_objective("f1", make_f_beta(1.0))
register_objective("f_beta", make_f_beta(0.5))
register_objective("precision_at_recall", make_precision_at_recall(0.30))
register_objective("expected_value", expected_value)
register_objective("dot_product", make_dot_product())
register_objective("dot_product_cvar", make_dot_product(risk_source="cvar"))
register_objective("roc_auc", lambda x: x.roc_auc if x.roc_auc is not None else MISSING_PENALTY)
