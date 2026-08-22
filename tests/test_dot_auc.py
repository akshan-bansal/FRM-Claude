from __future__ import annotations

import pytest

from trading_live_claude.analysis.matrix import MatrixCell
from trading_live_claude.scoring.objective import (
    DEFAULT_METRIC_WEIGHTS,
    MISSING_PENALTY,
    ObjectiveInput,
    get_objective,
    make_dot_product,
)
from trading_live_claude.scoring.selection import cell_objective_input, rank_cells


def _cell(strategy: str, **kw: float) -> MatrixCell:
    base: dict[str, float] = dict(recall=0.5, specificity=0.5, precision=0.5, max_drawdown=-0.1, roc_auc=0.5)
    base.update(kw)
    return MatrixCell(strategy=strategy, symbol="X", num_trades=20, support=500, **base)  # type: ignore[arg-type]


def test_dot_product_is_weighted_vector() -> None:
    x = ObjectiveInput(sharpe=0.0, max_drawdown=-0.2, recall=0.4, specificity=0.8, precision=0.6, fidelity=0.5)
    # 5-D vector = [sens .4, spec .8, prec .6, risk (1-0.2)=0.8, fidelity .5]
    expected = 0.20 * 0.4 + 0.20 * 0.8 + 0.25 * 0.6 + 0.15 * 0.8 + 0.20 * 0.5
    assert get_objective("dot_product")(x) == pytest.approx(expected)


def test_dot_product_fidelity_floored_at_zero() -> None:
    # Isolate fidelity: a negative (inverted) edge contributes 0, a positive one passes.
    only_fid = make_dot_product({"fidelity": 1.0})
    base = dict(sharpe=0.0, max_drawdown=0.0, recall=0.0, specificity=0.0, precision=0.0)
    assert only_fid(ObjectiveInput(**base, fidelity=-0.9)) == pytest.approx(0.0)
    assert only_fid(ObjectiveInput(**base, fidelity=0.7)) == pytest.approx(0.7)


def test_dot_product_custom_weights() -> None:
    x = ObjectiveInput(sharpe=0.0, max_drawdown=0.0, recall=1.0, specificity=0.0, precision=0.0)
    only_sens = make_dot_product({"sensitivity": 1.0})
    assert only_sens(x) == pytest.approx(1.0)


def test_dot_product_penalizes_unmeasured() -> None:
    x = ObjectiveInput(sharpe=1.0, max_drawdown=-0.1)  # no precision/recall
    assert get_objective("dot_product")(x) <= MISSING_PENALTY


def test_default_weights_sum_to_one() -> None:
    assert sum(DEFAULT_METRIC_WEIGHTS.values()) == pytest.approx(1.0)


def test_roc_auc_objective_reads_cell() -> None:
    x = cell_objective_input(_cell("a", roc_auc=0.73))
    assert get_objective("roc_auc")(x) == pytest.approx(0.73)


def test_rank_cells_by_dot_product() -> None:
    strong = _cell("strong", recall=0.8, specificity=0.8, precision=0.8, max_drawdown=-0.05, roc_auc=0.9)
    weak = _cell("weak", recall=0.1, specificity=0.2, precision=0.1, max_drawdown=-0.5, roc_auc=0.5)
    ranked = rank_cells([weak, strong], objective="dot_product")
    assert ranked[0][0].strategy == "strong"


def test_rank_cells_by_roc_auc() -> None:
    hi = _cell("hi", roc_auc=0.95)
    lo = _cell("lo", roc_auc=0.55)
    ranked = rank_cells([lo, hi], objective="roc_auc")
    assert [c.strategy for c, _ in ranked] == ["hi", "lo"]


def test_rank_cells_accepts_custom_weighted_callable() -> None:
    # Custom weights: precision only → the high-precision cell must win regardless
    # of its (deliberately worse) other axes.
    hi = _cell("hi", precision=0.9, recall=0.0, specificity=0.0, max_drawdown=-0.9)
    lo = _cell("lo", precision=0.1, recall=0.9, specificity=0.9, max_drawdown=0.0)
    ranked = rank_cells([lo, hi], objective=make_dot_product({"precision": 1.0}))
    assert ranked[0][0].strategy == "hi"
    assert ranked[0][1] == pytest.approx(0.9)
