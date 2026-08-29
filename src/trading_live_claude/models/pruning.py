"""Feature pruning by greedy forward selection.

The cross-sectional experiment showed the failure mode of small data: throwing every factor at a
gradient-boosted model overfit and the out-of-sample rank IC collapsed. The fix is to *prune* — keep
only the features that earn their place. This does greedy forward selection: start empty, repeatedly
add the single feature that most improves an out-of-sample score, and stop when the best remaining
addition no longer clears ``min_gain``. It is model-agnostic — you pass a ``score`` callable that
takes a feature subset and returns a number to maximize (e.g. an out-of-fold rank IC), so the same
pruner works for any estimator.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PruneResult:
    selected: list[str]                    # the kept features, in the order they were added
    path: list[tuple[str, float]]          # (feature added, score after adding) at each step
    full_score: float                      # score using all features (the un-pruned baseline)

    @property
    def best_score(self) -> float:
        return self.path[-1][1] if self.path else 0.0


def forward_select(features: Sequence[str], score: Callable[[list[str]], float], *,
                   min_gain: float = 0.001, max_features: int | None = None) -> PruneResult:
    """Greedily add features that improve ``score``; stop when the best addition gains < ``min_gain``.

    ``score`` is called with a list of feature names and returns a value to maximize. Returns the
    selected subset, the step-by-step path, and the all-features baseline for comparison.
    """
    remaining = list(features)
    selected: list[str] = []
    path: list[tuple[str, float]] = []
    best = score([])                       # empty-model baseline: a feature must beat holding nothing
    while remaining and (max_features is None or len(selected) < max_features):
        scored = [(f, score([*selected, f])) for f in remaining]
        f_best, s_best = max(scored, key=lambda kv: kv[1])
        if s_best <= best + min_gain:
            break
        selected.append(f_best)
        remaining.remove(f_best)
        best = s_best
        path.append((f_best, s_best))
    full = score(list(features))
    return PruneResult(selected=selected, path=path, full_score=full)
