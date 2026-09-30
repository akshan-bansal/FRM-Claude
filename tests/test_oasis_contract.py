"""Seed/result contract for the OASIS VM boundary."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading_live_claude.sim import RunBudget, build_seed, ingest_result

BUDGET = RunBudget(max_usd=5, max_agents=50, max_steps=10)


def _overlay(tmp: Path, ages: dict[str, float]) -> Path:
    p = tmp / "intel_overlay.jsonl"
    snap = {"strategic_risk": 65.0, "energy_stress": 0.29, "event_acceleration": {"energy": 1.0},
            "fear_greed": 54.7, "source_age_hours": ages}
    p.write_text(json.dumps({"as_of": "2026-09-29T00:00:00+00:00", "snapshot": snap}) + "\n",
                 encoding="utf-8")
    return p


def _result(run_id: str, **over) -> dict:
    r = {"run_id": run_id, "finished_at": "2026-09-29T01:00:00+00:00", "completed_steps": 10,
         "agents": 50, "model": BUDGET.model, "spend_usd": 4.2, "input_tokens": 1, "output_tokens": 1,
         "stopped_by": "steps",
         "stances": [{"symbol": "BTC/USD", "mean": -0.2, "dispersion": 0.3, "agents": 50}]}
    r.update(over)
    return r


def _issue(tmp: Path, ages: dict[str, float] | None = None):
    seed = build_seed(_overlay(tmp, ages or {"news": 0.1}), ["BTC/USD"], BUDGET)
    (tmp / "seeds").mkdir()
    (tmp / "seeds" / f"{seed.run_id}.json").write_text(seed.model_dump_json(), encoding="utf-8")
    return seed


def _write(tmp: Path, r: dict) -> Path:
    p = tmp / "result.json"
    p.write_text(json.dumps(r), encoding="utf-8")
    return p


def test_stale_source_is_withheld_not_flagged(tmp_path: Path) -> None:
    seed = build_seed(_overlay(tmp_path, {"news": 0.1, "energy": 135.0}), ["BTC/USD"], BUDGET)
    assert "energy_stress" not in seed.snapshot and "event_acceleration" not in seed.snapshot
    assert "source:energy" in seed.withheld and "energy_stress" in seed.withheld
    assert seed.snapshot["fear_greed"] == 54.7


def test_roundtrip_and_duplicate_rejected(tmp_path: Path) -> None:
    seed = _issue(tmp_path)
    journal = tmp_path / "runs.jsonl"
    ingest_result(_write(tmp_path, _result(seed.run_id)), tmp_path / "seeds", journal)
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 1
    with pytest.raises(ValueError, match="already ingested"):
        ingest_result(_write(tmp_path, _result(seed.run_id)), tmp_path / "seeds", journal)


def test_over_budget_result_is_rejected(tmp_path: Path) -> None:
    seed = _issue(tmp_path)
    with pytest.raises(ValueError, match="budget"):
        ingest_result(_write(tmp_path, _result(seed.run_id, spend_usd=9.0)),
                      tmp_path / "seeds", tmp_path / "runs.jsonl")


def test_result_without_a_seed_is_rejected(tmp_path: Path) -> None:
    _issue(tmp_path)
    with pytest.raises(ValueError, match="no seed"):
        ingest_result(_write(tmp_path, _result("deadbeef0000")), tmp_path / "seeds",
                      tmp_path / "runs.jsonl")


def test_errored_or_empty_run_is_not_stored(tmp_path: Path) -> None:
    seed = _issue(tmp_path)
    with pytest.raises(ValueError):
        ingest_result(_write(tmp_path, _result(seed.run_id, stopped_by="error", stances=[])),
                      tmp_path / "seeds", tmp_path / "runs.jsonl")
    assert not (tmp_path / "runs.jsonl").exists()
