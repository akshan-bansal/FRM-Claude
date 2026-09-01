from __future__ import annotations

import json
from pathlib import Path

from trading_live_claude.intel.reliability import MIN_SAMPLE, assess


def _journal(tmp: Path, reads: list[dict]) -> Path:
    p = tmp / "j.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for i, r in enumerate(reads):
            fh.write(json.dumps({
                "as_of": f"2026-08-{10 + i:02d}T12:00:00+00:00",
                "snapshot": {
                    "strategic_risk": r.get("risk", 50.0),
                    "conflict_events_active": r.get("conflict", 2.0),
                    "global_alert_count": r.get("alerts", 3.0),
                    "market": {"equity_vol": r.get("vix", 20.0)},
                    "source_age_hours": {"news": r.get("age", 1.0)},
                    "degraded": r.get("degraded", False),
                },
            }) + "\n")
    return p


def test_no_verdict_below_minimum_sample(tmp_path: Path) -> None:
    """An instrument is not assessed from a handful of readings."""
    q = assess(_journal(tmp_path, [{"risk": 50.0}] * 4))
    assert not q.has_verdict
    assert "no verdict" in q.summary()


def test_availability_counts_degraded_reads(tmp_path: Path) -> None:
    reads = [{"risk": 50.0}] * 8 + [{"risk": 0.0, "degraded": True}] * 2
    q = assess(_journal(tmp_path, reads))
    assert q.availability.n == 10
    assert abs(q.availability.value - 0.8) < 1e-9


def test_test_retest_rewards_stable_and_punishes_jitter(tmp_path: Path) -> None:
    """A slow-moving index should agree between consecutive reads; a jittery one should not."""
    stable = [{"risk": 50.0 + i * 0.5} for i in range(12)]          # smooth drift
    jitter = [{"risk": 50.0 if i % 2 else 90.0} for i in range(12)]  # alternating extremes
    q_stable = assess(_journal(tmp_path / "a", stable)) if (tmp_path / "a").mkdir() is None else None
    q_jitter = assess(_journal(tmp_path / "b", jitter)) if (tmp_path / "b").mkdir() is None else None
    assert q_stable is not None and q_jitter is not None
    assert q_stable.test_retest["strategic_risk"].value > q_jitter.test_retest["strategic_risk"].value


def test_internal_consistency_flags_an_unexpected_sign(tmp_path: Path) -> None:
    """Conflict escalations feed the strategic-risk index by construction.

    An inverse relationship is a red flag about the instrument, not a finding about the world.
    """
    inverted = [{"conflict": float(i), "risk": 90.0 - i * 3.0} for i in range(12)]
    q = assess(_journal(tmp_path, inverted))
    stat = q.internal_consistency["conflict_events_active~strategic_risk"]
    assert stat.value < 0
    assert "UNEXPECTED" in stat.note


def test_criterion_validity_measures_against_an_external_yardstick(tmp_path: Path) -> None:
    """When the feed says risk is up, does the market independently agree?"""
    agreeing = [{"risk": 40.0 + i * 4.0, "vix": 14.0 + i * 1.5} for i in range(12)]
    q = assess(_journal(tmp_path, agreeing))
    stat = q.criterion_validity["strategic_risk~vix"]
    assert stat.value > 0.9 and stat.usable


def test_missing_journal_is_handled(tmp_path: Path) -> None:
    q = assess(tmp_path / "nope.jsonl")
    assert q.n_reads == 0 and not q.has_verdict


def test_min_sample_is_enforced_consistently() -> None:
    assert MIN_SAMPLE >= 10
