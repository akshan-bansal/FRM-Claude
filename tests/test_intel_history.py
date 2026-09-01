from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from trading_live_claude.intel.history import (
    IntelHistory,
    derive,
    load_journal,
)


def _write_journal(tmp: Path, series: list[dict]) -> Path:
    """Write a synthetic journal; each entry is (strategic_risk, energy accel, degraded)."""
    p = tmp / "intel.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for i, s in enumerate(series):
            rec = {
                "as_of": f"2026-08-{10 + i:02d}T12:00:00+00:00",
                "snapshot": {
                    "strategic_risk": s.get("risk", 50.0),
                    "fear_greed": s.get("fg", 50.0),
                    "event_acceleration": {"energy": s.get("accel", 1.0)},
                    "market": {"equity_vol": s.get("vix", 20.0)},
                    "country_alert_counts": {"UA": 1},
                    "degraded": s.get("degraded", False),
                },
                "decisions": {"commodity": {"scalar": s.get("scalar", 1.0), "halt": False}},
            }
            fh.write(json.dumps(rec) + "\n")
    return p


def test_load_journal_builds_a_time_series(tmp_path: Path) -> None:
    p = _write_journal(tmp_path, [{"risk": 50.0}, {"risk": 60.0}, {"risk": 70.0}])
    df = load_journal(p)
    assert len(df) == 3
    assert isinstance(df.index, pd.DatetimeIndex) and df.index.is_monotonic_increasing
    assert df["strategic_risk"].tolist() == [50.0, 60.0, 70.0]
    assert "accel_energy" in df.columns          # dict field flattened
    assert "scalar_commodity" in df.columns      # journaled decision captured
    assert df["n_countries"].iloc[-1] == 1.0


def test_degraded_records_are_excluded(tmp_path: Path) -> None:
    """A partial fetch reads as a fake drop in risk; including it would corrupt every delta."""
    p = _write_journal(tmp_path, [{"risk": 70.0}, {"risk": 0.0, "degraded": True}, {"risk": 72.0}])
    df = load_journal(p)
    assert len(df) == 2
    assert 0.0 not in df["strategic_risk"].tolist()


def test_thin_history_degrades_to_neutral_instead_of_inventing(tmp_path: Path) -> None:
    """Below MIN_HISTORY the relative features must claim nothing — deltas are still fine."""
    p = _write_journal(tmp_path, [{"risk": 50.0}, {"risk": 90.0}])
    feats = derive(load_journal(p), min_history=8)
    risk = feats["strategic_risk"]
    assert risk.latest == 90.0
    assert risk.delta == 40.0        # a two-point delta is honest
    assert risk.pct_rank == 0.5      # ...but position and persistence are not
    assert risk.run_length == 0
    assert risk.trend == "flat"


def test_relative_features_engage_once_history_is_deep_enough(tmp_path: Path) -> None:
    rising = [{"risk": r} for r in (40, 42, 44, 46, 48, 60, 70, 85)]
    feats = derive(load_journal(_write_journal(tmp_path, rising)), min_history=8)
    risk = feats["strategic_risk"]
    assert risk.latest == 85.0
    assert risk.delta == 15.0
    assert risk.pct_rank > 0.9       # highest reading we have on record
    assert risk.trend == "rising"
    assert risk.run_length >= 3      # sustained above its own median


def test_persistence_distinguishes_a_spike_from_a_regime(tmp_path: Path) -> None:
    """The whole point: one 6x reading is noise, the same reading sustained is a regime."""
    spike = [{"accel": 1.0}] * 7 + [{"accel": 6.0}]        # elevated on the final read only
    regime = [{"accel": 1.0}] * 3 + [{"accel": 6.0}] * 5   # elevated across five consecutive reads
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    f_spike = derive(load_journal(_write_journal(a, spike)), min_history=8)
    f_regime = derive(load_journal(_write_journal(b, regime)), min_history=8)
    assert f_spike["accel_energy"].run_length < f_regime["accel_energy"].run_length


def test_intel_history_reports_its_own_usability(tmp_path: Path) -> None:
    thin = IntelHistory(_write_journal(tmp_path, [{"risk": 50.0}] * 3), min_history=8)
    assert thin.depth == 3 and not thin.is_usable
    assert "need 8" in thin.summary()

    deep = IntelHistory(_write_journal(tmp_path, [{"risk": 50.0}] * 10), min_history=8)
    assert deep.depth == 10 and deep.is_usable
    assert deep.span_hours() > 0


def test_missing_journal_is_not_an_error(tmp_path: Path) -> None:
    h = IntelHistory(tmp_path / "does_not_exist.jsonl")
    assert h.depth == 0 and not h.is_usable
    assert h.get("strategic_risk") is None
    assert "empty" in h.summary()
