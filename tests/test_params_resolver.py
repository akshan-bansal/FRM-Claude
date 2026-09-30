"""Tests for analysis/params.py — the live-strategy params precedence chain (2026-09-18)."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

import trading_live_claude.analysis.universe as universe
from trading_live_claude.analysis.params import build_strategy, resolve_params, running_params
from trading_live_claude.intel.notification import format_entry


@dataclass
class _Rec:
    strategy: str
    params: dict = field(default_factory=dict)
    tier: str = "robust"
    asset_class: str = "equity"
    wfe: float = 1.2
    oos_score: float = 3.0
    oos_return: float = 0.2
    oos_max_drawdown: float = -0.1
    oos_trades: int = 12
    oos_win_rate: float | None = None


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> dict:
    reg = {"AAA": _Rec("ts_momentum", {"lookback": 189, "threshold": 0.02}),
           "BBB": _Rec("bollinger", {"window": 10, "n_std": 1.5}),
           "BAD": _Rec("ts_momentum", {"not_a_param": 1})}
    monkeypatch.setattr(universe, "WALK_FORWARD_VALIDATED", reg)
    return reg


def test_default_mode_is_class_defaults(registry: dict) -> None:
    kwargs, source, _ = resolve_params("ts_momentum", "AAA", "default")
    assert (kwargs, source) == ({}, "default")
    r = build_strategy("ts_momentum", "AAA", "default")
    assert running_params(r.strategy)["lookback"] == 126


def test_wf_mode_uses_the_registry_when_it_covers_this_strategy(registry: dict) -> None:
    r = build_strategy("ts_momentum", "AAA", "wf")
    assert r.source == "wf"
    assert running_params(r.strategy)["lookback"] == 189
    assert running_params(r.strategy)["threshold"] == 0.02


def test_wf_mode_falls_through_when_the_registry_is_for_another_strategy(registry: dict) -> None:
    # BBB's registry entry is bollinger; asking for rsi_meanrevert must not borrow its params
    kwargs, source, note = resolve_params("rsi_meanrevert", "BBB", "wf")
    assert source in ("calibrated", "default")
    assert "no registry entry" in note
    assert "n_std" not in kwargs


def test_params_the_constructor_rejects_fall_back_to_defaults(registry: dict) -> None:
    r = build_strategy("ts_momentum", "BAD", "wf")
    assert r.source == "default" and "rejected" in r.note
    assert running_params(r.strategy)["lookback"] == 126


def test_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError):
        resolve_params("ts_momentum", "AAA", "latest")


def test_entry_alert_shows_running_params_and_flags_a_mismatch() -> None:
    rec = _Rec("ts_momentum", {"lookback": 189, "threshold": 0.02})
    _, body = format_entry(strategy_name="ts_momentum", symbol="AAA", price=10.0, detail={},
                           wf_record=rec, live_params={"lookback": 126, "threshold": 0.0, "scale": 0.25})
    assert "Evidence is for: ts_momentum with lookback=189, threshold=0.02" in body
    assert "Running: ts_momentum with lookback=126, threshold=0.0" in body
    assert "MISMATCH" in body
    _, same = format_entry(strategy_name="ts_momentum", symbol="AAA", price=10.0, detail={},
                           wf_record=rec, live_params={"lookback": 189, "threshold": 0.02, "scale": 0.25})
    assert "Running: ts_momentum with lookback=189, threshold=0.02" in same
    assert "MISMATCH" not in same
