"""Odds conversions and de-vigging.

Decimal odds ``d`` pay ``stake * d`` (stake included), so the market-implied probability of an
outcome is ``1/d``. Across the outcomes of one market a bookmaker's implied probabilities sum to
*more* than 1 — the excess is the **overround** (vig / margin), the book's built-in edge and the
direct analog of a transaction cost. Removing it (de-vigging) renormalizes the implied
probabilities to sum to 1, giving that book's *fair* probability estimate.
"""
from __future__ import annotations

from collections.abc import Sequence


def American_to_decimal(american: int) -> float:
    """US moneyline (e.g. -150, +200) to decimal odds."""
    return 1.0 + (american / 100.0 if american > 0 else 100.0 / -american)


def implied_prob(decimal_odds: float) -> float:
    """Market-implied probability of an outcome priced at ``decimal_odds`` (= 1/odds)."""
    if decimal_odds <= 1.0:
        raise ValueError("decimal odds must be > 1.0")
    return 1.0 / decimal_odds


def overround(odds: Sequence[float]) -> float:
    """Bookmaker margin over one market's outcomes: ``sum(1/odds) - 1`` (0 = fair, >0 = vig)."""
    return sum(implied_prob(o) for o in odds) - 1.0


def devig(odds: Sequence[float]) -> list[float]:
    """Fair probabilities for one market's outcomes — implied probabilities renormalized to sum to 1
    (proportional de-vig). Strips the overround so the numbers are a genuine probability estimate."""
    imp = [implied_prob(o) for o in odds]
    total = sum(imp)
    if total <= 0:
        raise ValueError("odds produced non-positive total implied probability")
    return [p / total for p in imp]


def fair_odds(prob: float) -> float:
    """Break-even decimal odds for a probability (= 1/p)."""
    if not 0.0 < prob <= 1.0:
        raise ValueError("prob must be in (0, 1]")
    return 1.0 / prob
