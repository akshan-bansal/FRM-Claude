from __future__ import annotations

import pytest

from trading_live_claude.betting import (
    American_to_decimal,
    consensus_fair_probs,
    detect_arbitrage,
    devig,
    implied_prob,
    overround,
    value_bets,
)


def test_odds_conversions_and_overround() -> None:
    assert implied_prob(2.0) == 0.5
    assert abs(American_to_decimal(-150) - (1 + 100 / 150)) < 1e-9
    assert American_to_decimal(200) == 3.0
    assert overround([2.0, 2.0]) == 0.0                     # fair two-way
    assert abs(overround([1.9, 1.9]) - (2 / 1.9 - 1)) < 1e-9  # ~5.3% vig
    with pytest.raises(ValueError):
        implied_prob(1.0)


def test_devig_normalizes_to_one() -> None:
    fair = devig([1.9, 1.9])
    assert abs(sum(fair) - 1.0) < 1e-12 and fair == [0.5, 0.5]
    skew = devig([1.5, 3.0])
    assert abs(sum(skew) - 1.0) < 1e-12 and skew[0] > skew[1]


def test_no_arbitrage_when_books_carry_vig() -> None:
    arb = detect_arbitrage({"a": [1.9, 1.9], "b": [1.85, 1.95]})
    assert not arb.is_arb and arb.total_implied > 1.0


def test_arbitrage_detected_across_books() -> None:
    # book A best on outcome 0 (2.10), book B best on outcome 1 (2.10) -> implied 0.952 < 1
    arb = detect_arbitrage({"A": [2.10, 1.80], "B": [1.80, 2.10]}, outcomes=["home", "away"])
    assert arb.is_arb
    assert arb.best_book == ["A", "B"] and arb.best_odds == [2.10, 2.10]
    assert abs(arb.profit_margin - (1 / (2 / 2.10) - 1)) < 1e-9   # ~5% guaranteed
    assert abs(sum(arb.stakes) - 1.0) < 1e-12
    # equal-payout check: each outcome's stake*odds is the same
    payouts = [s * o for s, o in zip(arb.stakes, arb.best_odds, strict=True)]
    assert abs(payouts[0] - payouts[1]) < 1e-12


def test_consensus_fair_probs_pool_the_books() -> None:
    fair = consensus_fair_probs({"a": [1.9, 1.9], "b": [2.0, 1.8]})
    assert abs(sum(fair) - 1.0) < 1e-9 and 0.4 < fair[0] < 0.6


def test_value_bets_flag_above_fair_prices_with_kelly() -> None:
    # fair 50/50; a soft book offers 2.2 on outcome 0 -> +EV
    bets = value_bets({"soft": [2.2, 1.75], "sharp": [1.95, 1.95]},
                      fair_probs=[0.5, 0.5], outcomes=["h", "a"], kelly_fraction=0.5, min_edge=0.0)
    assert bets and bets[0].book == "soft" and bets[0].outcome == "h"
    assert abs(bets[0].edge - (0.5 * 2.2 - 1)) < 1e-9        # +0.10 edge
    assert abs(bets[0].kelly_stake - (0.1 / 1.2) * 0.5) < 1e-9
    # a fairly-priced 2.0 (edge 0) is not flagged
    none = value_bets({"fair": [2.0, 2.0]}, fair_probs=[0.5, 0.5])
    assert none == []
