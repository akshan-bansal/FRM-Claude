"""Runs ON THE VM. Reads a seed, drives OASIS, writes a result file when it finishes.

This is the wrapper the sim was missing: it owns the exit point (the result file is written in a
``finally``) and enforces the seed's budget. The driver itself is not written yet, because the OASIS
API has not been exercised from this repo; ``drive`` raises until it is filled in and tried on a
tiny budget. Needs ANTHROPIC_API_KEY in the VM's environment, never in this repo's .env.

    python oasis_runner.py seed.json --out result.json --confirm-spend
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


class Meter:
    """Running spend. ``drive`` must call ``add`` after every model call and stop on ``over``."""

    def __init__(self, max_usd: float) -> None:
        self.max_usd, self.usd, self.tin, self.tout = max_usd, 0.0, 0, 0

    def add(self, usd: float, tin: int, tout: int) -> None:
        self.usd += usd
        self.tin += tin
        self.tout += tout

    @property
    def over(self) -> bool:
        return self.usd >= self.max_usd


def drive(seed: dict, meter: Meter) -> tuple[int, list[dict], str]:
    """Return (completed_steps, stances, stopped_by). Fill in against camel-oasis, then test cheap."""
    raise NotImplementedError("OASIS driver not written: try it on a $1 budget before trusting it")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("seed", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--confirm-spend", action="store_true")
    a = ap.parse_args()
    seed = json.loads(a.seed.read_text(encoding="utf-8"))
    b = seed["budget"]
    if not a.confirm_spend:
        print(f"dry run: would spend up to ${b['max_usd']} on {b['model']}; pass --confirm-spend")
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set on this VM", file=sys.stderr)
        return 2
    meter, steps, stances, stopped = Meter(b["max_usd"]), 0, [], "error"
    try:
        steps, stances, stopped = drive(seed, meter)
    finally:
        a.out.write_text(json.dumps({
            "run_id": seed["run_id"], "finished_at": datetime.now(UTC).isoformat(),
            "completed_steps": steps, "agents": b["max_agents"], "model": b["model"],
            "spend_usd": round(meter.usd, 4), "input_tokens": meter.tin, "output_tokens": meter.tout,
            "stopped_by": stopped, "stances": stances}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
