"""Gradient-boosted-trees selection model — predict out-of-sample score from in-sample features.

The pool has been selected one name at a time: run the in-sample panel, walk-forward the winner,
keep it if it clears the gate. This learns the *pattern* across all those decisions — a
gradient-boosted-trees regressor that maps a candidate's cheap **in-sample** features (best panel
score, score dispersion, volatility, liquidity, trade count, momentum) to its **out-of-sample**
score. Once trained it pre-ranks new candidates before the expensive walk-forward, and its
permutation importances say *which* in-sample signals actually predict out-of-sample survival.

Two honesty guards are built into how it's used, not hidden:
* **out-of-fold evaluation** — the headline metric is the rank information coefficient (Spearman of
  cross-validated predictions vs actual), never the in-sample fit, so a model that only memorizes
  scores badly and is caught.
* **small-N regularization** — shallow trees, an L2 penalty and a minimum leaf size, because the
  training set is only as large as the number of names walk-forwarded so far.

Needs the optional ``ml`` extra (scikit-learn).
"""
from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pandas as pd

# The in-sample feature columns the model expects (all computable before any walk-forward).
FEATURES: tuple[str, ...] = (
    "is_best_score", "is_mean_score", "is_std_score", "is_pos_fraction",
    "best_trades", "annual_vol", "log_price", "log_dollar_vol", "is_total_return",
)


class SelectionModel:
    """Wraps ``HistGradientBoostingRegressor`` with out-of-fold scoring and permutation importance."""

    def __init__(self, *, max_depth: int = 3, max_iter: int = 250, learning_rate: float = 0.05,
                 l2_regularization: float = 1.0, min_samples_leaf: int = 5, random_state: int = 0) -> None:
        from sklearn.ensemble import HistGradientBoostingRegressor

        self._params = dict(max_depth=max_depth, max_iter=max_iter, learning_rate=learning_rate,
                            l2_regularization=l2_regularization, min_samples_leaf=min_samples_leaf,
                            random_state=random_state)
        self.model = HistGradientBoostingRegressor(**self._params)
        self.features_: list[str] = []

    def fit(self, X: pd.DataFrame, y: npt.ArrayLike) -> SelectionModel:
        self.features_ = list(X.columns)
        self.model.fit(X.to_numpy(dtype=float), np.asarray(y, dtype=float))
        return self

    def predict(self, X: pd.DataFrame) -> npt.NDArray[np.float64]:
        return np.asarray(self.model.predict(X[self.features_].to_numpy(dtype=float)), dtype=float)

    def out_of_fold_predict(self, X: pd.DataFrame, y: npt.ArrayLike, *, cv: int = 5) -> npt.NDArray[np.float64]:
        """Cross-validated predictions — each row predicted by a model that never saw it."""
        from sklearn.base import clone
        from sklearn.model_selection import KFold, cross_val_predict

        n = len(X)
        splits = KFold(n_splits=min(cv, n), shuffle=True, random_state=0)
        preds = cross_val_predict(clone(self.model), X.to_numpy(dtype=float), np.asarray(y, dtype=float), cv=splits)
        return np.asarray(preds, dtype=float)

    def rank_ic(self, X: pd.DataFrame, y: npt.ArrayLike, *, cv: int = 5) -> float:
        """Out-of-fold rank information coefficient (Spearman of CV predictions vs actual)."""
        from scipy.stats import spearmanr

        oof = self.out_of_fold_predict(X, y, cv=cv)
        rho = spearmanr(oof, np.asarray(y, dtype=float)).correlation
        return float(rho) if np.isfinite(rho) else 0.0

    def permutation_importance(self, X: pd.DataFrame, y: npt.ArrayLike, *, n_repeats: int = 30) -> dict[str, float]:
        """Mean permutation importance per feature (higher = the model leans on it more), fit on all rows."""
        from sklearn.inspection import permutation_importance as _pi

        self.fit(X, y)
        r = _pi(self.model, X.to_numpy(dtype=float), np.asarray(y, dtype=float), n_repeats=n_repeats, random_state=0)
        return dict(sorted(zip(self.features_, r.importances_mean, strict=True), key=lambda kv: -kv[1]))
