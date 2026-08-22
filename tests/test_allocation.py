from __future__ import annotations

import pytest

from trading_live_claude.risk.allocation import (
    Candidate,
    allocation_weights,
    risk_weighted_allocation,
)


def _cands() -> list[Candidate]:
    return [
        Candidate("LOWRISK", price=100.0, cvar=0.02, score=0.5),
        Candidate("HIGHRISK", price=100.0, cvar=0.10, score=0.5),
        Candidate("HIGHSCORE", price=100.0, cvar=0.05, score=0.9),
    ]


@pytest.mark.parametrize("method", ["equal_risk", "risk_parity", "score", "score_cvar"])
def test_weights_sum_to_one(method: str) -> None:
    w = allocation_weights(_cands(), method=method)
    assert sum(w.values()) == pytest.approx(1.0)
    assert all(v >= 0 for v in w.values())


def test_risk_parity_favours_lower_cvar() -> None:
    w = allocation_weights(_cands(), method="risk_parity")
    assert w["LOWRISK"] > w["HIGHRISK"]  # thinner tail → more capital


def test_score_method_favours_higher_score() -> None:
    w = allocation_weights(_cands(), method="score", temperature=0.2)
    assert w["HIGHSCORE"] == max(w.values())


def test_score_cvar_combines_edge_and_tail() -> None:
    # HIGHSCORE has the best score but middling tail; score_cvar should still rank it
    # above the equal-score names, and LOWRISK above HIGHRISK.
    w = allocation_weights(_cands(), method="score_cvar", temperature=0.3)
    assert w["LOWRISK"] > w["HIGHRISK"]
    assert w["HIGHSCORE"] > w["HIGHRISK"]


def test_allocation_sizes_to_cvar_stop() -> None:
    positions = risk_weighted_allocation(
        [Candidate("X", price=100.0, cvar=0.05, score=1.0)],
        equity=100_000, risk_budget_pct=0.05,
    )
    assert len(positions) == 1
    p = positions[0]
    # dollar_risk = 100k * 5% = 5000; stop_distance = 0.05*100 = 5; shares = 1000
    assert p.dollar_risk == pytest.approx(5000.0)
    assert p.shares == 1000
    assert p.stop == pytest.approx(95.0)


def test_higher_tail_gets_fewer_shares() -> None:
    positions = risk_weighted_allocation(
        [Candidate("LOW", 100.0, 0.02, 0.5), Candidate("HIGH", 100.0, 0.10, 0.5)],
        equity=100_000, risk_budget_pct=0.05, method="risk_parity",
    )
    by = {p.symbol: p for p in positions}
    assert by["LOW"].shares > by["HIGH"].shares  # low tail → bigger position


def test_max_positions_caps_and_renormalizes() -> None:
    positions = risk_weighted_allocation(_cands(), equity=100_000, max_positions=2)
    assert len(positions) <= 2
    assert sum(p.weight for p in positions) == pytest.approx(1.0)


def test_zero_equity_and_empty_safe() -> None:
    assert risk_weighted_allocation(_cands(), equity=0.0) == []
    assert risk_weighted_allocation([], equity=100_000) == []
    assert allocation_weights([]) == {}
