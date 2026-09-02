"""Off-cadence graph-poll runner — one canonical polling path shared with the live trading loop.

Uses :class:`trading_live_claude.intel.routing.OverlayProvider` — the SAME poller the live monitor
uses when it is running with ``signal --intel-overlay``. Each ``refresh`` computes overlay
decisions AND journals the snapshot via :func:`intel.history.append_snapshot`, which writes both
the flat overlay row AND the graph edges (aggregate + per-event). Trading loops and this overnight
runner therefore share one code path — no duplicate WorldMonitor client wiring, no duplicate
journal path, no drift between them.

Snapshots are cached with a ``cached_at`` stamp on the vendor side; running at too high a cadence
just journals the SAME payload against a stale age. The default ``--sleep 900`` (15 minutes)
matches the vendor's typical refresh interval, which is also OverlayProvider's default
``refresh_seconds``.

Nothing here is on the hot path — the trading loops instantiate their OWN OverlayProvider inside
the CLI. This process is a separate instance dedicated to accretion when no trading loop is up.

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
    wash_journal_file,
)
from trading_live_claude.intel.overlay import IntelSnapshot
from trading_live_claude.intel.routing import OverlayProvider
from trading_live_claude.intel.worldmonitor import WorldMonitorClient


def _build_overlay_provider(refresh_seconds: float) -> OverlayProvider:
    """OverlayProvider expects a synchronous zero-arg snapshot_fn; wrap the async client."""
    s = get_settings()

    def snapshot_fn() -> IntelSnapshot:
        async def _one() -> IntelSnapshot:
            async with WorldMonitorClient(s.worldmonitor_api_key) as wm:
                return await wm.snapshot()
        return asyncio.run(_one())

    return OverlayProvider(snapshot_fn, refresh_seconds=refresh_seconds, journal=True)


def _one_snapshot(provider: OverlayProvider) -> None:
    """Force a refresh — bypass the throttle so our --sleep cadence is what counts."""
    provider.refresh(force=True)


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
    ap.add_argument("--wash-every", type=int, default=0,
                    help="Run the temporal wash+prune (intel.graph.wash_journal_file) every N "
                         "polls, keeping the journal from growing without bound. 0 = never (safe "
                         "default for testing). 4 at --sleep 900 = about hourly.")
    args = ap.parse_args()

    provider = _build_overlay_provider(refresh_seconds=args.sleep)
    before = _profile_graph()
    print(f"[thicken] start: {before['edges_total']} edges, {before['nodes_total']} nodes",
          flush=True)
    print(f"[thicken] nodes by type: {before['nodes_by_type']}", flush=True)
    print(f"[thicken] polling via OverlayProvider (canonical live-path poller) "
          f"— overlay decisions computed AND journaled per refresh", flush=True)

    for i in range(1, args.iterations + 1):
        t0 = time.time()
        try:
            _one_snapshot(provider)
        except Exception as e:
            print(f"[thicken] iter {i}: FAILED - {e}", flush=True)
        else:
            snap_profile = _profile_graph()
            grew_edges = snap_profile["edges_total"] - before["edges_total"]
            print(f"[thicken] iter {i}/{args.iterations}: journal now has "
                  f"{snap_profile['edges_total']} edges (+{grew_edges} since start) in "
                  f"{time.time() - t0:.1f}s", flush=True)
        # Temporal gate: prune old edges + decay weights per-predicate. Runs AFTER the poll's
        # own write, so the wash sees what was just journaled and applies the age policy in one
        # pass. Atomic + backed up by wash_journal_file, so a mid-write crash cannot corrupt.
        if args.wash_every and i % args.wash_every == 0:
            summary = wash_journal_file()
            print(f"[thicken] wash: {summary['before']} -> {summary['after']} edges "
                  f"(pruned {summary['pruned']})", flush=True)
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
