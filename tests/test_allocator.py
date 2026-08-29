from __future__ import annotations

import numpy as np
import pandas as pd

from trading_live_claude.portfolio import PortfolioAllocator


def _returns(seed: int, n: int = 500, vol: float = 0.01) -> pd.Series:
    return pd.Series(np.random.default_rng(seed).normal(0.0, vol, n))


def test_weights_sum_to_gross_and_leave_cash() -> None:
    rets = {"A": _returns(1), "B": _returns(2), "C": _returns(3)}
    scores = {"A": 5.0, "B": 4.0, "C": 3.0}
    res = PortfolioAllocator(max_weight=1.0).allocate(rets, scores, regime_scalar=0.6)
    assert abs(sum(res.weights.values()) - 0.6) < 1e-6
    assert abs(res.gross_exposure - 0.6) < 1e-6 and abs(res.cash - 0.4) < 1e-6


def test_correlated_pair_splits_one_slot() -> None:
    """Two identical (corr=1) names should together get about one uncorrelated name's weight."""
    s = _returns(10)
    rets = {"A": s, "B": s.copy(), "C": _returns(11)}   # A,B perfectly correlated; C independent
    scores = {"A": 5.0, "B": 5.0, "C": 5.0}             # equal edge and (same) vol
    w = PortfolioAllocator(max_weight=1.0).allocate(rets, scores, regime_scalar=1.0).weights
    assert abs((w["A"] + w["B"]) - w["C"]) < 0.05        # the pair combined ~= the standalone
    assert abs(w["A"] - w["C"] / 2) < 0.05


def test_per_name_cap_respected() -> None:
    rets = {n: _returns(i) for i, n in enumerate("ABCDE")}
    scores = {"A": 10.0, "B": 1.0, "C": 1.0, "D": 1.0, "E": 1.0}  # A would dominate uncapped
    res = PortfolioAllocator(max_weight=0.30).allocate(rets, scores, regime_scalar=1.0)
    assert max(res.weights.values()) <= 0.30 + 1e-6


def test_sleeve_cap_limits_a_sleeve() -> None:
    rets = {n: _returns(i) for i, n in enumerate("ABCD")}
    scores = {n: 5.0 for n in "ABCD"}
    sleeves = {"A": "crypto", "B": "crypto", "C": "equity", "D": "equity"}
    res = PortfolioAllocator(max_weight=1.0, max_sleeve_weight=0.3).allocate(
        rets, scores, regime_scalar=1.0, sleeves=sleeves)
    assert res.sleeve_weights.get("crypto", 0.0) <= 0.3 + 1e-6


def test_nonpositive_scores_get_no_weight_and_empty_is_all_cash() -> None:
    rets = {"A": _returns(1), "B": _returns(2)}
    res = PortfolioAllocator().allocate(rets, {"A": 5.0, "B": -1.0}, regime_scalar=1.0)
    assert "B" not in res.weights and "A" in res.weights
    empty = PortfolioAllocator().allocate(rets, {"A": 0.0, "B": -1.0})
    assert empty.weights == {} and empty.cash == 1.0
