from __future__ import annotations

import numpy as np
import pandas as pd

from trading_live_claude.models import FEATURES, SelectionModel


def _dataset(n: int = 80, seed: int = 0) -> tuple[pd.DataFrame, np.ndarray]:
    """A feature matrix where the OOS target is driven mostly by is_best_score (plus noise)."""
    rng = np.random.default_rng(seed)
    X = pd.DataFrame({f: rng.normal(0, 1, n) for f in FEATURES})
    y = 3.0 * X["is_best_score"].to_numpy() + 0.5 * X["annual_vol"].to_numpy() + rng.normal(0, 0.5, n)
    return X, y


def test_fit_predict_shapes() -> None:
    X, y = _dataset()
    m = SelectionModel().fit(X, y)
    p = m.predict(X)
    assert p.shape == (len(X),) and np.isfinite(p).all()
    assert m.features_ == list(FEATURES)


def test_out_of_fold_rank_ic_recovers_signal() -> None:
    X, y = _dataset()
    ic = SelectionModel().rank_ic(X, y, cv=5)
    assert ic > 0.4                       # honest OOF ranking, not memorization


def test_permutation_importance_finds_the_driver() -> None:
    X, y = _dataset()
    imp = SelectionModel().permutation_importance(X, y, n_repeats=15)
    top = next(iter(imp))                  # highest-importance feature
    assert top == "is_best_score"
    assert imp["is_best_score"] > imp["log_price"]
