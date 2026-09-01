from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trading_live_claude.intel.events import event_intensity
from trading_live_claude.intel.overlay import IntelSnapshot, OverlayConfig, RiskOverlay

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _rec(days_ago: float, country: str = "UA") -> dict:
    ts = int((NOW - timedelta(days=days_ago)).timestamp() * 1000)
    return {"ingestedAt": ts, "occurredAt": ts - 900_000, "country": country}


def test_acceleration_detects_a_surge() -> None:
    # 20 events in the last 3 days vs 10 spread over the prior 27 -> clear pickup
    recs = [_rec(d) for d in [0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0,
                              2.2, 2.4, 2.6, 2.7, 2.8, 2.85, 2.9, 2.93, 2.96, 2.99]]
    recs += [_rec(d) for d in range(4, 14)]
    ei = event_intensity(recs, domain="conflict", recent_days=3.0, now=NOW)
    assert ei.recent_count == 20
    assert ei.acceleration > 2.0
    assert ei.span_days > 10
    assert ei.countries.get("UA") == 30


def test_quiet_flow_is_not_flagged() -> None:
    recs = [_rec(d) for d in (0.5, 5.0, 10.0, 15.0, 20.0)]
    ei = event_intensity(recs, domain="energy", recent_days=3.0, now=NOW)
    assert ei.acceleration < 1.5      # below the gate's accel_lo -> no de-risk


def test_no_older_records_never_fabricates_a_spike() -> None:
    """With only recent data there is no baseline, so acceleration must be a neutral 1.0."""
    ei = event_intensity([_rec(0.1), _rec(0.2)], domain="conflict", recent_days=3.0, now=NOW)
    assert ei.acceleration == 1.0


def test_empty_and_malformed_records_are_safe() -> None:
    assert event_intensity([], domain="conflict").acceleration == 1.0
    bad = [{"ingestedAt": None, "occurredAt": "not-a-number"}, {}]
    assert event_intensity(bad, domain="conflict").recent_count == 0


def test_accel_gate_de_risks_but_only_as_a_gentle_tilt() -> None:
    ov = RiskOverlay()
    calm = ov.evaluate(IntelSnapshot(event_acceleration={"conflict": 1.0}))["equity"].scalar
    surge = ov.evaluate(IntelSnapshot(event_acceleration={"conflict": 6.0}))["equity"].scalar
    assert surge < calm
    assert surge >= OverlayConfig().accel_floor      # a tilt, never a stop
    assert OverlayConfig().accel_floor == 0.75


def test_sparse_older_tail_cannot_fabricate_a_spike() -> None:
    """A baseline built from one or two stale records is not a baseline.

    Regression: before the guard, 39 recent events plus a single 30-day-old one produced a 351x
    acceleration, enough to drive the overlay to HALT on pure noise.
    """
    recs = [_rec(i * 0.07) for i in range(39)] + [_rec(30.0)]
    ei = event_intensity(recs, domain="energy", recent_days=3.0, now=NOW)
    assert ei.acceleration == 1.0          # too thin to claim anything

    # with a real baseline the ratio is computed, but clipped against the heavy tail
    real = [_rec(i * 0.07) for i in range(39)] + [_rec(20 + i) for i in range(8)]
    ei2 = event_intensity(real, domain="energy", recent_days=3.0, now=NOW)
    assert 1.0 < ei2.acceleration <= 10.0


def test_acceleration_is_clipped_at_the_cap() -> None:
    recs = [_rec(i * 0.01) for i in range(60)] + [_rec(25 + i) for i in range(6)]
    ei = event_intensity(recs, domain="conflict", recent_days=3.0, now=NOW,
                         min_baseline_events=5, max_acceleration=4.0)
    assert ei.acceleration == 4.0
