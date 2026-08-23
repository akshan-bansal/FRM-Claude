from __future__ import annotations

import pytest

from trading_live_claude.backtest.metrics import Metrics
from trading_live_claude.scoring.objective import (
    MISSING_PENALTY,
    OBJECTIVES,
    ObjectiveAdapter,
    ObjectiveInput,
    get_objective,
    make_f_beta,
    make_precision_at_recall,
)


def _inp(**kw: float | int | None) -> ObjectiveInput:
    base: dict[str, float | int | None] = {"sharpe": 1.0, "max_drawdown": -0.1}
    base.update(kw)
    return ObjectiveInput(**base)  # type: ignore[arg-type]


def test_adapter_swaps_by_name() -> None:
    x = _inp(precision=0.8, recall=0.4)
    assert ObjectiveAdapter.from_name("precision").score(x) == pytest.approx(0.8)
    assert ObjectiveAdapter.from_name("recall").score(x) == pytest.approx(0.4)


def test_unknown_objective_lists_options() -> None:
    with pytest.raises(KeyError) as ei:
        get_objective("does_not_exist")
    assert "precision" in str(ei.value)


def test_sharpe_over_dd_matches_legacy_formula() -> None:
    x = _inp(sharpe=2.0, max_drawdown=-0.2)
    assert get_objective("sharpe_over_dd")(x) == pytest.approx(2.0 / 0.2)


def test_sortino_over_dd_uses_downside_ratio() -> None:
    x = _inp(sortino=3.0, sharpe=1.0, max_drawdown=-0.2)
    assert get_objective("sortino_over_dd")(x) == pytest.approx(3.0 / 0.2)


def test_classification_objectives_penalize_unmeasured_runs() -> None:
    x = _inp(precision=None, recall=None)  # no labels supplied
    for name in ("precision", "recall", "f1", "f_beta", "expected_value"):
        assert get_objective(name)(x) <= MISSING_PENALTY


def test_precision_at_recall_floor() -> None:
    par = make_precision_at_recall(0.5)
    below = _inp(precision=0.9, recall=0.4)
    above = _inp(precision=0.6, recall=0.7)
    # A high-precision run that misses the recall floor must lose to a passing one.
    assert par(below) < par(above)
    assert par(above) == pytest.approx(0.6)


def test_f_beta_factory_weighting() -> None:
    x = _inp(precision=0.5, recall=0.9)
    assert make_f_beta(0.5)(x) < make_f_beta(2.0)(x)


def test_expected_value_uses_2to1_rr() -> None:
    # EV = 3*precision - 1; break-even precision is 1/3.
    assert get_objective("expected_value")(_inp(precision=1 / 3)) == pytest.approx(0.0)
    assert get_objective("expected_value")(_inp(precision=0.5)) == pytest.approx(0.5)


def test_from_metrics_bridges_backtest_layer() -> None:
    m = Metrics(0, 0, 1.5, 0, -0.1, 0.5, 20, 0.01, 0.3, precision=0.7, recall=0.6)
    x = ObjectiveInput.from_metrics(m)
    assert x.precision == 0.7 and x.recall == 0.6 and x.sharpe == 1.5


def test_registry_is_enumerable() -> None:
    assert {"precision", "recall", "sharpe_over_dd"}.issubset(OBJECTIVES)
