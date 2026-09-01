"""Iteratively accrete OSINT edges into ``state/intel_graph.jsonl`` — the deliberate off-cadence
runner for the "iteratively developing vertices/edges" posture (NEXT_SESSION.md #6).

Each iteration fetches one live WorldMonitor snapshot and lets the existing wiring do the writes:

* ``intel.history.append_snapshot`` writes the flat overlay row (as always)
* ``intel.graph.append_snapshot_edges`` writes aggregate edges from snapshot fields
* ``intel.worldmonitor._write_event_edges`` writes per-event ``mentioned_by`` / ``about_domain`` /
  ``affects_region`` edges from the raw signals + advisories + conflict sample

Snapshots are cached with a ``cached_at`` stamp on the vendor side; running this at too high a
cadence just journals the SAME payload repeatedly with a stale age, thickening nothing new. The
default ``--sleep 900`` (15 minutes) matches the vendor's typical refresh interval and keeps the
edges honest.

Nothing here is on the hot path. Trading loops are unaffected — this is a research tool.

Usage examples::

    # 20 snapshots at the vendor's refresh cadence
    python scripts/thicken_graph.py --iterations 20

    # short smoke test that the wiring works end-to-end
    python scripts/thicken_graph.py --iterations 2 --sleep 5

At the end it prints the vertex/edge profile of the journal so growth is visible without needing a
second command.
"""
from __future__ import annotations

import argparse
import asyncio
import time
from collections import Counter
from pathlib import Path

from trading_live_claude.config import get_settings
from trading_live_claude.intel.graph import (
    DEFAULT_GRAPH_JOURNAL,
    load_edges,
)
from trading_live_claude.intel.history import append_snapshot
from trading_live_claude.intel.overlay import RiskOverlay
from trading_live_claude.intel.worldmonitor import WorldMonitorClient


async def _one_snapshot() -> None:
    s = get_settings()
    async with WorldMonitorClient(s) as wm:
        snap = await wm.snapshot()
    # append_snapshot handles both flat + graph writes.
    append_snapshot(snap, RiskOverlay().evaluate(snap))


def _profile_graph(path: str | Path = DEFAULT_GRAPH_JOURNAL) -> dict[str, object]:
    """Vertex/edge summary of the current journal — what the overlay & interpreter can read."""
    edges = load_edges(path)
    nodes: set[tuple[str, str]] = set()
    per_type: Counter[str] = Counter()
    per_pred: Counter[str] = Counter()
    for e in edges:
        nodes.add(e.subject)
        nodes.add(e.object)
        per_type[e.subject[0]] += 1
        per_type[e.object[0]] += 1
        per_pred[e.predicate] += 1
    node_by_type: Counter[str] = Counter(n[0] for n in nodes)
    return {
        "edges_total": len(edges),
        "nodes_total": len(nodes),
        "nodes_by_type": dict(node_by_type),
        "edge_endpoints_by_type": dict(per_type),
        "edges_by_predicate": dict(per_pred),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=8,
                    help="How many snapshot fetches to run.")
    ap.add_argument("--sleep", type=float, default=900.0,
                    help="Seconds between snapshots. Vendor cache is ~15 min; anything shorter "
                         "re-writes the same payload against a stale age.")
    args = ap.parse_args()

    before = _profile_graph()
    print(f"[thicken] start: {before['edges_total']} edges, {before['nodes_total']} nodes",
          flush=True)
    print(f"[thicken] nodes by type: {before['nodes_by_type']}", flush=True)

    for i in range(1, args.iterations + 1):
        t0 = time.time()
        try:
            asyncio.run(_one_snapshot())
        except Exception as e:
            print(f"[thicken] iter {i}: FAILED — {e}", flush=True)
        else:
            snap_profile = _profile_graph()
            grew_edges = snap_profile["edges_total"] - before["edges_total"]
            print(f"[thicken] iter {i}/{args.iterations}: journal now has "
                  f"{snap_profile['edges_total']} edges (+{grew_edges} since start) in "
                  f"{time.time() - t0:.1f}s", flush=True)
        if i < args.iterations:
            time.sleep(args.sleep)

    after = _profile_graph()
    print(f"\n[thicken] final profile:")
    print(f"    edges total     {after['edges_total']:>7}   (+{after['edges_total'] - before['edges_total']})")
    print(f"    nodes total     {after['nodes_total']:>7}   (+{after['nodes_total'] - before['nodes_total']})")
    print(f"    nodes by type   {after['nodes_by_type']}")
    print(f"    edges by pred   {after['edges_by_predicate']}")


if __name__ == "__main__":
    main()
