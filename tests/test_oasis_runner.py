"""The VM runner's guards that need no OASIS install: price, budget and dry-run behaviour."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "oasis_runner", Path(__file__).resolve().parent.parent / "vm" / "oasis_runner.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def _seed(**budget) -> dict:
    b = {"max_usd": 5.0, "max_agents": 50, "max_steps": 10, "model": "claude-haiku-4-5-20251001"}
    b.update(budget)
    return {"run_id": "r1", "as_of": "2026-09-30T00:00:00+00:00", "symbols": ["BTC/USD"],
            "snapshot": {"strategic_risk": 65.0, "fear_greed": 54.7}, "withheld": {}, "budget": b}


def test_unknown_model_is_refused_before_anything_is_imported_or_spent() -> None:
    with pytest.raises(RuntimeError, match="no price for"):
        runner.drive(_seed(model="some-unpriced-model"), runner.Meter(5.0))


def test_budget_that_cannot_cover_one_step_is_refused() -> None:
    with pytest.raises(RuntimeError, match="cannot cover one step"):
        runner.drive(_seed(max_usd=0.05), runner.Meter(0.05))


def test_brief_uses_only_fields_the_seed_holds() -> None:
    seed = _seed()
    assert "strategic risk: 65.0" in runner._brief(seed) and "energy" not in runner._brief(seed)
    seed["snapshot"] = {}
    assert runner._brief(seed) == "no quantitative readings"


def test_step_cost_carries_the_safety_factor() -> None:
    one = runner._step_cost(1, (1.0, 5.0))
    assert one == pytest.approx((4000 * 1.0 + 300 * 5.0) / 1e6 * runner.SAFETY)


def test_dry_run_spends_nothing_and_writes_no_result(tmp_path: Path, monkeypatch, capsys) -> None:
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(_seed()), encoding="utf-8")
    out = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv", ["oasis_runner.py", str(seed), "--out", str(out)])
    assert runner.main() == 0
    assert "dry run" in capsys.readouterr().out and not out.exists()


def test_missing_key_is_refused_even_with_confirm_spend(tmp_path: Path, monkeypatch) -> None:
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps(_seed()), encoding="utf-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["oasis_runner.py", str(seed), "--out", str(tmp_path / "r.json"),
                                      "--confirm-spend"])
    assert runner.main() == 2
