"""Seed / result contract for the OASIS VM run.

Seed (out): the latest overlay snapshot with every source older than ``MAX_SOURCE_AGE_H`` withheld,
and a hard spend budget the VM runner must enforce. Result (in): what the run reported, validated
before it is appended to ``state/oasis_runs.jsonl``.

Data the simulation cannot be given faithfully is withheld, not flagged: a stale source is dropped
from the seed and named in ``withheld``. A result that fails validation is rejected, not stored with
a warning. The result carries stances and spend as the run reported them; nothing here says the
simulated stances were right.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

MAX_SOURCE_AGE_H = 48.0


class RunBudget(BaseModel):
    """Hard caps the VM runner must enforce and report against."""

    max_usd: float = Field(gt=0, le=200, description="Stop the run when reported spend reaches this.")
    max_agents: int = Field(gt=0, le=1000)
    max_steps: int = Field(gt=0, le=200)
    model: str = "claude-haiku-4-5-20251001"


class SimSeed(BaseModel):
    run_id: str
    created_at: str
    as_of: str
    symbols: list[str]
    snapshot: dict[str, Any]
    withheld: dict[str, str]          # field -> why it was left out of the seed
    budget: RunBudget


class Stance(BaseModel):
    symbol: str
    mean: float = Field(ge=-1, le=1)  # -1 adverse .. +1 constructive, across simulated agents
    dispersion: float = Field(ge=0)
    agents: int = Field(gt=0)


class SimResult(BaseModel):
    run_id: str
    finished_at: str
    completed_steps: int = Field(ge=0)
    agents: int = Field(gt=0)
    model: str
    spend_usd: float = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    stopped_by: str                   # "steps" | "budget" | "error"
    stances: list[Stance]

    @model_validator(mode="after")
    def _has_content(self) -> SimResult:
        if self.stopped_by == "error" or not self.stances:
            raise ValueError("a run that errored or produced no stances is not stored as a result")
        return self


def build_seed(overlay_journal: Path, symbols: list[str], budget: RunBudget) -> SimSeed:
    """Seed from the last overlay row. Raises if the journal has none: no specimen seed exists."""
    rows = [ln for ln in overlay_journal.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not rows:
        raise ValueError(f"{overlay_journal} has no rows to seed from")
    row = json.loads(rows[-1])
    snap = dict(row["snapshot"])
    ages = snap.get("source_age_hours") or {}
    stale = {k: v for k, v in ages.items() if v is not None and v > MAX_SOURCE_AGE_H}
    withheld = {f"source:{k}": f"{v:.0f}h old, limit {MAX_SOURCE_AGE_H:.0f}h" for k, v in stale.items()}
    if "energy" in stale:
        for f in ("energy_stress", "event_acceleration"):
            if f in snap:
                withheld[f] = "derived from the stale energy source"
                snap.pop(f)
    return SimSeed(run_id=uuid.uuid4().hex[:12], created_at=datetime.now(UTC).isoformat(),
                   as_of=row["as_of"], symbols=symbols, snapshot=snap, withheld=withheld,
                   budget=budget)


def ingest_result(result_path: Path, seeds_dir: Path, out_journal: Path) -> SimResult:
    """Validate a result file against its seed's budget, then append it once to the journal."""
    res = SimResult.model_validate_json(result_path.read_text(encoding="utf-8"))
    seed_file = seeds_dir / f"{res.run_id}.json"
    if not seed_file.exists():
        raise ValueError(f"no seed for run {res.run_id}: results only count against a seed we issued")
    seed = SimSeed.model_validate_json(seed_file.read_text(encoding="utf-8"))
    b = seed.budget
    if res.spend_usd > b.max_usd * 1.05 or res.agents > b.max_agents or res.completed_steps > b.max_steps:
        raise ValueError("result exceeds the seed's budget: the runner did not enforce its caps")
    if out_journal.exists() and any(f'"run_id":"{res.run_id}"' in ln.replace(" ", "")
                                    for ln in out_journal.read_text(encoding="utf-8").splitlines()):
        raise ValueError(f"run {res.run_id} is already ingested")
    row = {"seed": seed.model_dump(), "result": res.model_dump()}
    out_journal.parent.mkdir(parents=True, exist_ok=True)
    with out_journal.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return res
