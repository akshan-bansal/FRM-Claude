"""Issue a seed for the OASIS VM run, and ingest its result. Spends nothing itself.

    python scripts/oasis_sim.py seed --max-usd 5 --max-agents 50 --max-steps 10
    python scripts/oasis_sim.py ingest state/oasis_inbox/<run_id>.result.json
    python scripts/oasis_sim.py ingest state/oasis_inbox/result.json --db state/oasis_inbox/oasis_<run_id>.db

``seed`` writes state/oasis_seeds/<run_id>.json; copy it to the VM. Copy the runner's result back
into state/oasis_inbox/ and run ``ingest``. The API key stays on the VM.

With ``--db`` (the run's own ``oasis_<run_id>.db``) ingest also writes the run to the intel graph
journal, once: the poll that briefed the society, its reported stances, and its follows, engagement
and symbol attention read from the database. Without it only the result is recorded.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from trading_live_claude.sim import RunBudget, SimSeed, build_seed, ingest_result, journal_run

STATE = Path("state")
DEFAULT_SYMBOLS = "BTC/USD,ETH/USD,SOL/USD,XRP/USD,LINK/USD,XMR/USD"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--max-usd", type=float, required=True)
    s.add_argument("--max-agents", type=int, required=True)
    s.add_argument("--max-steps", type=int, required=True)
    s.add_argument("--model", default=RunBudget.model_fields["model"].default)
    s.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    i = sub.add_parser("ingest")
    i.add_argument("result", type=Path)
    i.add_argument("--db", type=Path, default=None,
                   help="the run's oasis_<run_id>.db; also journals its structure to the graph")
    i.add_argument("--graph-journal", type=Path, default=STATE / "intel_graph.jsonl")
    a = ap.parse_args()
    if a.cmd == "seed":
        seed = build_seed(STATE / "intel_overlay.jsonl", [x.strip() for x in a.symbols.split(",")],
                          RunBudget(max_usd=a.max_usd, max_agents=a.max_agents,
                                    max_steps=a.max_steps, model=a.model))
        out = STATE / "oasis_seeds" / f"{seed.run_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(seed.model_dump_json(indent=2), encoding="utf-8")
        print(f"seed {seed.run_id} -> {out}  (budget ${seed.budget.max_usd:g}, "
              f"{seed.budget.max_agents} agents, {seed.budget.max_steps} steps)")
        for k, why in seed.withheld.items():
            print(f"  withheld {k}: {why}")
    else:
        res = ingest_result(a.result, STATE / "oasis_seeds", STATE / "oasis_runs.jsonl")
        print(f"ingested {res.run_id}: {res.completed_steps} steps, ${res.spend_usd:.2f}, "
              f"{len(res.stances)} symbols")
        if a.db is not None:
            seed = SimSeed.model_validate_json(
                (STATE / "oasis_seeds" / f"{res.run_id}.json").read_text(encoding="utf-8"))
            n = journal_run(seed, res, a.db, path=a.graph_journal)
            print(f"graph journal: {n} edges written to {a.graph_journal}" if n
                  else f"graph journal: run {res.run_id} was already there; nothing written")


if __name__ == "__main__":
    main()
