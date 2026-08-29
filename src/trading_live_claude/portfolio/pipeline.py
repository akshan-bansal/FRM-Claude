"""Book construction from the cross-sectional ranker — the ranker's edge feeds the allocator.

Once the cross-sectional GBT clears the noise floor (which it does on a broad universe), its live
prediction — each name's expected *relative* forward return — is exactly the "edge" the allocator
wants. This ties the pieces into one call: build the feature panel, fit the ranker for the latest
cross-section, read the market regime off a benchmark, and hand the ranker's scores to the
correlation-aware, regime-scaled :class:`PortfolioAllocator`. Only positive-edge names (the top
half the ranker favors) get weight, so the result is a long book of the model's picks, de-crowded
and sized to the regime.

Paper research only — this produces target weights, it places no orders.
"""
from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from ..models import CrossSectionalRanker, RegimeClassifier, build_panel
from .allocator import AllocationResult, PortfolioAllocator


def ranker_scores(prices: Mapping[str, pd.DataFrame], *, horizon: int = 21,
                  fundamentals: Mapping[str, pd.DataFrame] | None = None,
                  ranker: CrossSectionalRanker | None = None) -> dict[str, float]:
    """The ranker's predicted relative forward return per name for the latest cross-section."""
    panel = build_panel(dict(prices), horizon=horizon, fundamentals=dict(fundamentals) if fundamentals else None)
    return (ranker or CrossSectionalRanker()).fit_latest(panel, horizon=horizon)


def build_book(prices: Mapping[str, pd.DataFrame], *, horizon: int = 21,
               fundamentals: Mapping[str, pd.DataFrame] | None = None,
               regime_benchmark: pd.Series | None = None,
               sleeves: Mapping[str, str] | None = None,
               allocator: PortfolioAllocator | None = None,
               ranker: CrossSectionalRanker | None = None) -> AllocationResult:
    """Ranker edge → allocator weights: the full book in one call.

    ``regime_benchmark`` (a broad close series) scales gross exposure; ``sleeves`` maps symbol ->
    sleeve for the sleeve caps. Returns the allocator's :class:`AllocationResult`.
    """
    scores = ranker_scores(prices, horizon=horizon, fundamentals=fundamentals, ranker=ranker)
    returns = {s: prices[s]["close"].pct_change().dropna().reset_index(drop=True) for s in prices}
    regime = RegimeClassifier().classify(regime_benchmark).risk_scalar if regime_benchmark is not None else 1.0
    alloc = allocator or PortfolioAllocator()
    return alloc.allocate(returns, scores, regime_scalar=regime, sleeves=sleeves)
