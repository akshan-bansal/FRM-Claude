"""Cross-book arbitrage (the sure bet).

For a market, take the *best* (highest) decimal odds available for each outcome across all books.
If ``sum(1/best_odds)`` — the combined implied probability at those best prices — is **below 1**,
you can back every outcome and lock in a profit whatever the result: this is the exact cross-venue
edge detector from the trading side, with the outcomes' implied probabilities in place of two
venues' quotes. Staking each outcome in proportion to its implied probability equalizes the payout,
so the return is guaranteed regardless of which outcome wins.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .odds import implied_prob


@dataclass(frozen=True)
class ArbOpportunity:
    outcomes: list[str]
    best_book: list[str]          # which book to bet each outcome at
    best_odds: list[float]        # the best decimal odds taken per outcome
    total_implied: float          # sum(1/best_odds); < 1.0 means an arb exists
    profit_margin: float          # guaranteed ROI on total stake (1/total_implied - 1)
    stakes: list[float]           # fraction of bankroll on each outcome (sums to 1)

    @property
    def is_arb(self) -> bool:
        return self.total_implied < 1.0 and self.profit_margin > 0.0


def detect_arbitrage(book_odds: Mapping[str, Sequence[float]], *,
                     outcomes: Sequence[str] | None = None) -> ArbOpportunity:
    """Best-price-per-outcome arbitrage across ``book_odds`` (book -> odds per outcome, same order).

    Returns an :class:`ArbOpportunity`; check ``.is_arb``. ``profit_margin`` is the guaranteed ROI,
    and ``stakes`` are the bankroll fractions that equalize the payout across outcomes.
    """
    books = list(book_odds)
    n = len(next(iter(book_odds.values())))
    if any(len(v) != n for v in book_odds.values()):
        raise ValueError("every book must quote the same number of outcomes")
    names = list(outcomes) if outcomes is not None else [f"outcome_{i}" for i in range(n)]

    best_odds: list[float] = []
    best_book: list[str] = []
    for i in range(n):
        b = max(books, key=lambda bk: book_odds[bk][i])
        best_book.append(b)
        best_odds.append(float(book_odds[b][i]))

    total = sum(implied_prob(o) for o in best_odds)
    profit = (1.0 / total - 1.0) if total > 0 else 0.0
    stakes = [implied_prob(o) / total for o in best_odds]
    return ArbOpportunity(outcomes=names, best_book=best_book, best_odds=best_odds,
                          total_implied=total, profit_margin=profit, stakes=stakes)
