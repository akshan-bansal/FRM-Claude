from __future__ import annotations

from trading_live_claude.models import forward_select


def test_forward_select_keeps_only_useful_features() -> None:
    good = {"A", "B"}
    def score(feats: list[str]) -> float:
        return 0.1 * len(good.intersection(feats)) - 0.02 * len(set(feats) - good)  # noise hurts
    res = forward_select(["A", "B", "C", "D", "E"], score, min_gain=0.001)
    assert set(res.selected) == good                 # pruned to the signal-bearing pair
    assert res.best_score > res.full_score           # a pruned subset beats using everything
    assert len(res.path) == 2


def test_forward_select_respects_max_features() -> None:
    def score(feats: list[str]) -> float:
        return float(len(feats))                     # everything helps -> would take all
    res = forward_select(["A", "B", "C", "D"], score, min_gain=0.0, max_features=2)
    assert len(res.selected) == 2


def test_forward_select_empty_when_nothing_helps() -> None:
    res = forward_select(["A", "B"], lambda feats: 0.0, min_gain=0.001)
    assert res.selected == []
