"""Tests for intel/thesis_intensity.py — bounded log grading of thesis inputs over value and time."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from trading_live_claude.intel.overlay import IntelSnapshot
from trading_live_claude.intel.thesis_intensity import (
    MAGNITUDE_BOUNDS,
    THESIS_LEGS,
    TIME_BOUND,
    TIME_FLOOR,
    firing_hours,
    intensity,
    load_history,
    log_ramp,
    magnitude,
)

T0 = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _calm(**kw) -> IntelSnapshot:
    """Calm market, so Complacency divergence fires whenever strategic_risk clears its gate."""
    base = dict(market={"equity_vol": 15.0}, fear_greed=56.0,
                event_acceleration={"energy": 1.0, "conflict": 1.0, "military": 1.0})
    base.update(kw)
    return IntelSnapshot(**base)


# ---- the curve ---------------------------------------------------------------------------------

def test_log_ramp_is_bounded_zero_at_floor_one_at_ceiling() -> None:
    assert log_ramp(60.0, 60.0, 85.0) == 0.0
    assert log_ramp(85.0, 60.0, 85.0) == pytest.approx(1.0)
    assert log_ramp(10.0, 60.0, 85.0) == 0.0          # clamped below
    assert log_ramp(500.0, 60.0, 85.0) == pytest.approx(1.0)   # clamped above


def test_log_ramp_is_log10_shaped_with_default_curvature() -> None:
    # u = 0.1 -> log10(1.9); u = 0.5 -> log10(5.5)
    assert log_ramp(0.1, 0.0, 1.0) == pytest.approx(0.2788, abs=1e-4)
    assert log_ramp(0.5, 0.0, 1.0) == pytest.approx(0.7404, abs=1e-4)


def test_log_ramp_is_increasing_and_concave() -> None:
    xs = [i / 20 for i in range(21)]
    ys = [log_ramp(x, 0.0, 1.0) for x in xs]
    steps = [b - a for a, b in pairwise(ys)]
    assert all(s > 0 for s in steps)                              # strictly increasing
    assert all(b < a for a, b in pairwise(steps))                 # diminishing increments


def test_log_ramp_rejects_degenerate_bounds() -> None:
    with pytest.raises(ValueError):
        log_ramp(1.0, 5.0, 5.0)
    with pytest.raises(ValueError):
        log_ramp(1.0, 0.0, 1.0, k=0.0)


def test_every_modelled_leg_has_a_valid_bound() -> None:
    for _, legs in THESIS_LEGS.values():
        for leg in legs:
            b = MAGNITUDE_BOUNDS[leg]
            assert b.hi > b.lo and b.basis
    assert TIME_BOUND.hi > TIME_BOUND.lo


# ---- magnitude ---------------------------------------------------------------------------------

def test_or_thesis_takes_the_strongest_leg() -> None:
    snap = _calm(strategic_risk=67.0, event_acceleration={"energy": 4.0, "conflict": 1.0})
    mag, legs = magnitude("Complacency divergence", snap)
    assert mag == pytest.approx(max(s for _, s in legs.values()))
    assert legs["energy_accel"][1] > legs["strategic_risk"][1]


def test_and_thesis_takes_the_weakest_leg() -> None:
    snap = _calm(energy_stress=0.9, event_acceleration={"energy": 1.5})
    mag, legs = magnitude("Commodity carry-inversion proxy", snap)
    assert mag == pytest.approx(min(s for _, s in legs.values()))
    assert mag == pytest.approx(legs["energy_accel"][1])


def test_higher_input_never_lowers_magnitude() -> None:
    lo = magnitude("Complacency divergence", _calm(strategic_risk=67.0))[0]
    hi = magnitude("Complacency divergence", _calm(strategic_risk=74.0))[0]
    assert 0.0 < lo < hi <= 1.0


def test_unmodelled_thesis_returns_none_and_zero() -> None:
    assert magnitude("No notable configuration", _calm())[0] == 0.0
    assert intensity("Sentiment stretch — greed", _calm(fear_greed=80.0)) is None


# ---- time --------------------------------------------------------------------------------------

def _history(values: list[float], step_h: float = 1.0) -> list[tuple[datetime, IntelSnapshot]]:
    return [(T0 + timedelta(hours=i * step_h), _calm(strategic_risk=v)) for i, v in enumerate(values)]


def test_firing_hours_counts_the_current_streak_only() -> None:
    # fires, stops, then fires for the last 4 rows (3 hours between first and last of the streak)
    hist = _history([70, 70, 50, 70, 70, 70, 70])
    assert firing_hours("Complacency divergence", hist) == pytest.approx(3.0)


def test_firing_hours_breaks_on_a_gap_in_the_record() -> None:
    hist = [*_history([70, 70]), (T0 + timedelta(hours=10), _calm(strategic_risk=70.0)),
            (T0 + timedelta(hours=11), _calm(strategic_risk=70.0))]
    assert firing_hours("Complacency divergence", hist) == pytest.approx(1.0)


def test_firing_hours_is_zero_when_the_newest_row_does_not_fire() -> None:
    assert firing_hours("Complacency divergence", _history([70, 70, 50])) == 0.0
    assert firing_hours("Complacency divergence", []) == 0.0


# ---- combined ----------------------------------------------------------------------------------

def test_fresh_thesis_counts_at_the_time_floor_and_persistence_raises_it() -> None:
    snap = _calm(strategic_risk=70.0)
    fresh = intensity("Complacency divergence", snap)
    assert fresh is not None
    assert fresh.hours == 0.0
    assert fresh.intensity == pytest.approx(fresh.magnitude * TIME_FLOOR)

    held = intensity("Complacency divergence", snap, _history([70.0] * 25))   # 24h streak
    assert held is not None
    assert held.hours == pytest.approx(24.0)
    assert fresh.intensity < held.intensity < held.magnitude


def test_intensity_stays_within_zero_one() -> None:
    extreme = _calm(strategic_risk=100.0, event_acceleration={"energy": 50.0, "conflict": 9.0})
    hist = _history([100.0] * 100)
    r = intensity("Complacency divergence", extreme, hist)
    assert r is not None and 0.0 <= r.intensity <= 1.0
    assert r.intensity == pytest.approx(1.0)


def test_summary_names_both_factors_and_the_lead_leg() -> None:
    r = intensity("Complacency divergence", _calm(strategic_risk=70.0), _history([70.0] * 5))
    assert r is not None
    s = r.summary()
    assert "intensity" in s and "magnitude" in s and "lead: strategic_risk" in s and "4.0h firing" in s


# ---- journal loading ---------------------------------------------------------------------------

def test_load_history_skips_degraded_and_empty_rows(tmp_path: Path) -> None:
    p = tmp_path / "intel_overlay.jsonl"
    rows = [
        {"as_of": (T0).isoformat(), "snapshot": {"strategic_risk": 70.0, "market": {"equity_vol": 15.0}}},
        {"as_of": (T0 + timedelta(hours=1)).isoformat(),
         "snapshot": {"strategic_risk": 0.0, "market": {"crypto_chg": 9.0}, "degraded": False}},
        {"as_of": (T0 + timedelta(hours=2)).isoformat(),
         "snapshot": {"strategic_risk": 71.0, "degraded": True}},
        {"as_of": (T0 + timedelta(hours=3)).isoformat(), "snapshot": {"strategic_risk": 72.0}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    hist = load_history(p)
    assert [s.strategic_risk for _, s in hist] == [70.0, 72.0]
    assert load_history(tmp_path / "missing.jsonl") == []
