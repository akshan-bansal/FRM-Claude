"""Value (+EV) betting from a data-based fair line.

The prediction here is the market's own consensus, cleaned of margin: de-vig each book's line to a
probability, average across books, and that consensus is a strong estimate of the true outcome
probability (wisdom of the crowd of bookmakers). A book offering odds *better* than this fair line
is a positive-expected-value bet. Edge is ``p_fair * odds - 1`` (expected profit per unit staked),
and the fraction of bankroll to bet is the **Kelly** stake ``edge / (odds - 1)`` — scaled down by a
``kelly_fraction`` because the fair probability is an estimate, not truth.

This is the betting twin of the trading-side consensus-fair-value trick (the median-implied FX rate,
the interlisted USD/CAD): the crowd sets fair value, and you act on deviations from it.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .odds import devig


def consensus_fair_probs(book_odds: Mapping[str, Sequence[float]]) -> list[float]:
    """Consensus fair probability per outcome: de-vig each book's line, average across books.

    The de-vig removes each book's margin; averaging pools their views into one estimate that sums
    to 1. This is the ``prediction`` a value bet is measured against.
    """
    books = list(book_odds)
    n = len(next(iter(book_odds.values())))
    fair_each = [devig(book_odds[b]) for b in books]
    return [sum(f[i] for f in fair_each) / len(books) for i in range(n)]


@dataclass(frozen=True)
class ValueBet:
    outcome: str
    book: str
    odds: float
    fair_prob: float
    edge: float          # expected profit per unit stake = fair_prob * odds - 1
    kelly_stake: float   # fraction of bankroll (already scaled by kelly_fraction)


def value_bets(book_odds: Mapping[str, Sequence[float]], *, fair_probs: Sequence[float] | None = None,
               outcomes: Sequence[str] | None = None, kelly_fraction: float = 0.25,
               min_edge: float = 0.0) -> list[ValueBet]:
    """Positive-EV bets across ``book_odds`` vs a fair line (default: the de-vigged consensus).

    For every (book, outcome), the edge is ``fair_prob * odds - 1``; when it clears ``min_edge`` the
    bet is returned with a fractional-Kelly stake. Sorted by edge, best first.
    """
    books = list(book_odds)
    n = len(next(iter(book_odds.values())))
    fair = list(fair_probs) if fair_probs is not None else consensus_fair_probs(book_odds)
    names = list(outcomes) if outcomes is not None else [f"outcome_{i}" for i in range(n)]

    out: list[ValueBet] = []
    for b in books:
        for i in range(n):
            odds = float(book_odds[b][i])
            edge = fair[i] * odds - 1.0
            if edge > min_edge:
                kelly = max(0.0, edge / (odds - 1.0)) * kelly_fraction   # (bp - q)/b, fractional
                out.append(ValueBet(outcome=names[i], book=b, odds=odds, fair_prob=fair[i],
                                    edge=edge, kelly_stake=kelly))
    return sorted(out, key=lambda v: -v.edge)
