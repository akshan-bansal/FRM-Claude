from __future__ import annotations

from dataclasses import replace

from trading_live_claude.intel.confluence import (
    ATTENTION,
    build_evidence,
    confluence,
)
from trading_live_claude.intel.overlay import IntelSnapshot, RiskOverlay


def test_calm_world_scores_neutral() -> None:
    sc = confluence(IntelSnapshot(strategic_risk=50.0, fear_greed=50.0,
                                  market={"equity_vol": 15.0}))
    for s in sc.values():
        assert abs(s.score) < 5.0 and s.stance == "neutral"


def test_energy_shock_is_adverse_for_equity_and_constructive_for_commodity() -> None:
    """The reason this module exists, and a regression guard on a real sign bug.

    Severity is unsigned and the attention weight alone carries direction. An earlier version made
    BOTH signed, so the two cancelled and commodities scored adverse on exactly the supply shock
    that supports them — the inversion this test pins shut.
    """
    shock = IntelSnapshot(energy_stress=0.4, event_acceleration={"energy": 5.0})
    sc = confluence(shock)
    assert sc["commodity"].direction > 0, "commodities are long disruption"
    assert sc["equity"].direction < 0, "broad equity is short disruption"
    assert sc["commodity"].contributions["energy"] > 0
    assert sc["equity"].contributions["energy"] < 0


def test_it_can_disagree_with_the_de_risk_overlay() -> None:
    """Confluence is a second opinion, not a restatement — disagreement is the point."""
    shock = IntelSnapshot(energy_stress=0.4, event_acceleration={"energy": 6.0},
                          conflict_events_active=5)
    ov = RiskOverlay().evaluate(shock)
    sc = confluence(shock)
    assert ov["commodity"].scalar < 1.0          # overlay de-risks commodities
    assert sc["commodity"].direction > 0         # confluence reads them as supported


def test_staleness_discounts_confidence_not_direction() -> None:
    """An old reading means the same thing; we are just less sure of it."""
    shock = IntelSnapshot(energy_stress=0.4)
    fresh = confluence(shock)["commodity"]
    stale = confluence(replace(shock, source_age_hours={"energy": 72.0}))["commodity"]
    assert stale.confidence < fresh.confidence
    assert abs(stale.score) < abs(fresh.score)   # same sign, weaker claim
    assert stale.direction * fresh.direction > 0


def test_persistence_raises_confidence() -> None:
    """A condition held across journal reads is a regime; one reading is a data point."""
    shock = IntelSnapshot(energy_stress=0.4)
    once = build_evidence(shock, persistence={"energy": 0})["energy"]
    held = build_evidence(shock, persistence={"energy": 6})["energy"]
    assert held.confidence > once.confidence
    assert held.severity == once.severity        # persistence changes belief, not magnitude


def test_degraded_snapshot_is_weaker_evidence() -> None:
    shock = IntelSnapshot(energy_stress=0.4)
    assert confluence(replace(shock, degraded=True))["commodity"].confidence < \
        confluence(shock)["commodity"].confidence


def test_urgency_separates_fast_axes_from_slow_ones() -> None:
    """Event flow and vol move in hours; stress indices move in days and are never urgent."""
    snap = IntelSnapshot(energy_stress=0.4, event_acceleration={"energy": 5.0},
                         market={"equity_vol": 38.0})
    ev = build_evidence(snap)
    assert ev["event_flow"].urgency > ev["energy"].urgency
    assert ev["equity_vol"].urgency > ev["strategic_risk"].urgency


def test_scores_are_always_explainable() -> None:
    sc = confluence(IntelSnapshot(strategic_risk=90.0, conflict_events_active=7,
                                  market={"equity_vol": 35.0}))
    for s in sc.values():
        drivers = s.top_drivers(3)
        assert drivers and all(isinstance(k, str) for k, _ in drivers)
        assert -100.0 <= s.score <= 100.0
        assert 0.0 <= s.confidence <= 1.0 and 0.0 <= s.urgency <= 1.0


def test_attention_covers_every_class_and_dimension() -> None:
    dims = set(build_evidence(IntelSnapshot()).keys())
    for cls, weights in ATTENTION.items():
        assert set(weights) == dims, f"{cls} attention must span every evidence dimension"
