"""Risk-weighted portfolio position sizing.

The single-name ``PositionSizer`` risks a fixed fraction of equity per trade off an
ATR stop. This module sizes a *portfolio* of candidates at once, splitting one risk
budget across them so that riskier names get less capital — the essential companion
to the tail-risk metrics and the attention-style scoring.

Allocation methods (the ``QK^T`` → weights step of the attention analogy):
  * ``equal_risk``   — every position risks the same (1/N).
  * ``risk_parity``  — weight ∝ 1/CVaR: each position contributes equal *tail* risk.
  * ``score``        — softmax of the objective score (soft attention over the universe).
  * ``score_cvar``   — softmax(score)/CVaR: soft attention tilted by tail-risk parity
                       (the default — combines "what's the best edge" with "don't let
                       one fat tail dominate the book").

Each position is then sized to a **CVaR-based stop**: shares so that a tail-magnitude
(CVaR) adverse move loses exactly the position's allocated dollar risk.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

_MIN_CVAR = 1e-4  # floor so a (spuriously) zero tail doesn't get infinite weight


@dataclass(frozen=True)
class Candidate:
    """One asset up for allocation."""

    symbol: str
    price: float
    cvar: float   # per-period Expected Shortfall (positive fraction, e.g. 0.03)
    score: float  # objective score (the attention logit); higher = more attractive


@dataclass(frozen=True)
class AllocatedPosition:
    symbol: str
    weight: float        # normalized allocation weight in [0, 1]
    dollar_risk: float   # risk budget assigned to this position
    shares: int
    entry: float
    stop: float          # CVaR-based protective stop


def _softmax(xs: np.ndarray, temperature: float) -> np.ndarray:
    a = xs / max(temperature, 1e-9)
    a = a - a.max()
    e = np.exp(a)
    total = e.sum()
    return e / total if total > 0 else np.full_like(e, 1.0 / len(e))


def allocation_weights(
    candidates: Sequence[Candidate], *, method: str = "score_cvar", temperature: float = 1.0
) -> dict[str, float]:
    """Normalized allocation weights (sum to 1) for the chosen method."""
    if not candidates:
        return {}
    cvars = np.array([max(c.cvar, _MIN_CVAR) for c in candidates])
    scores = np.array([c.score for c in candidates], dtype=float)

    if method == "equal_risk":
        raw = np.ones(len(candidates))
    elif method == "risk_parity":
        raw = 1.0 / cvars
    elif method == "score":
        raw = _softmax(scores, temperature)
    elif method == "score_cvar":
        raw = _softmax(scores, temperature) / cvars
    else:
        raise ValueError(f"Unknown allocation method {method!r}")

    total = raw.sum()
    weights = raw / total if total > 0 else np.full(len(candidates), 1.0 / len(candidates))
    return {c.symbol: float(w) for c, w in zip(candidates, weights, strict=True)}


def risk_weighted_allocation(
    candidates: Sequence[Candidate],
    *,
    equity: float,
    risk_budget_pct: float = 0.05,
    method: str = "score_cvar",
    temperature: float = 1.0,
    max_positions: int | None = None,
) -> list[AllocatedPosition]:
    """Split ``equity * risk_budget_pct`` across candidates and size each to its CVaR stop.

    ``max_positions`` keeps only the top-N by weight (then re-normalizes), so the book
    stays concentrated in the strongest, thinnest-tailed names.
    """
    if equity <= 0 or not candidates:
        return []

    weights = allocation_weights(candidates, method=method, temperature=temperature)
    ranked = sorted(candidates, key=lambda c: weights[c.symbol], reverse=True)
    if max_positions is not None:
        ranked = ranked[:max_positions]

    # Re-normalize over the retained set.
    kept = np.array([weights[c.symbol] for c in ranked])
    kept = kept / kept.sum() if kept.sum() > 0 else np.full(len(ranked), 1.0 / len(ranked))

    total_risk = equity * risk_budget_pct
    out: list[AllocatedPosition] = []
    for c, w in zip(ranked, kept, strict=True):
        dollar_risk = total_risk * float(w)
        stop_distance = max(c.cvar, _MIN_CVAR) * c.price  # $ move for a CVaR-sized loss
        shares = max(math.floor(dollar_risk / stop_distance), 0) if stop_distance > 0 else 0
        if shares <= 0:
            continue
        out.append(
            AllocatedPosition(
                symbol=c.symbol,
                weight=float(w),
                dollar_risk=dollar_risk,
                shares=shares,
                entry=c.price,
                stop=c.price - stop_distance,
            )
        )
    return out
