"""Graph Journal — off-cadence poller that keeps ``state/intel_graph.jsonl`` growing.

Previously named "thickener"; the new name reflects what the process actually does — it IS the
graph journal's continuous-fetch counterpart, the read/write half of the OSINT graph that
otherwise would only see writes when a trading loop happened to be running.


Uses :class:`trading_live_claude.intel.routing.OverlayProvider` — the SAME poller the live monitor
uses when it is running with ``signal --intel-overlay``. Each ``refresh`` computes overlay
decisions AND journals the snapshot via :func:`intel.history.append_snapshot`, which writes both
the flat overlay row AND the graph edges (aggregate + per-event). Trading loops and this overnight
runner therefore share one code path — no duplicate WorldMonitor client wiring, no duplicate
journal path, no drift between them.

Two out-of-loop signals get piped to the trading-path Alerter (Telegram + email + stdout):

* **persistence hits** — when ``elevated_in`` for a domain has stood across N consecutive polls,
  once per crossing. Regime-detected signal. Threshold defaults to 5 polls (~1.25h at the
  default 900s cadence) and is per-domain de-duplicated so a signal that stays high for hours
  emits ONCE per crossing, not every poll.
* **wash events** — every temporal-gate run emits a one-line summary of edges pruned and
  fraction of the journal collapsed. Deliberately quiet: fires at the wash cadence (default
  once every 72h, not per poll).

Snapshots are cached with a ``cached_at`` stamp on the vendor side; running at too high a cadence
just journals the SAME payload against a stale age. Default ``--sleep 900`` (15 min) matches the
vendor's typical refresh interval.

Nothing here is on the hot path — the trading loops instantiate their OWN OverlayProvider inside
the CLI. This process is a separate instance dedicated to accretion when no trading loop is up.
"""
# renamed 2026-09-02 from scripts/thicken_graph.py
from __future__ import annotations

import argparse
import asyncio
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from trading_live_claude.config import get_settings
from trading_live_claude.intel.graph import (
    DEFAULT_GRAPH_JOURNAL,
    edge_persistence,
    load_edges,
    wash_journal_file,
)
from trading_live_claude.intel.interpret import THEME_EXEMPLARS, interpret
from trading_live_claude.intel.notification import (
    format_persistence,
    format_thesis,
    format_wash,
)
from trading_live_claude.intel.overlay import IntelSnapshot
from trading_live_claude.intel.routing import OverlayProvider
from trading_live_claude.intel.worldmonitor import WorldMonitorClient
from trading_live_claude.monitor import Alerter
from trading_live_claude.monitor.alerter import AlertConfig


# Domains we watch for elevated_in persistence — matches interpret.py's convention.
_WATCHED_DOMAINS: tuple[str, ...] = ("energy", "conflict", "military", "disaster", "economy")


def _build_overlay_provider(refresh_seconds: float) -> OverlayProvider:
    """OverlayProvider expects a synchronous zero-arg snapshot_fn; wrap the async client."""
    s = get_settings()

    def snapshot_fn() -> IntelSnapshot:
        async def _one() -> IntelSnapshot:
            async with WorldMonitorClient(s.worldmonitor_api_key) as wm:
                return await wm.snapshot()
        return asyncio.run(_one())

    return OverlayProvider(snapshot_fn, refresh_seconds=refresh_seconds, journal=True)


def _build_alerter() -> Alerter:
    s = get_settings()
    return Alerter(AlertConfig(
        telegram_bot_token=s.telegram_bot_token,
        telegram_chat_id=s.telegram_chat_id,
        smtp_host=s.smtp_host,
        smtp_user=s.smtp_user,
        smtp_pass=s.smtp_pass,
        email_to=s.alert_email_to,
    ))


def _one_snapshot(provider: OverlayProvider) -> None:
    """Force a refresh — bypass the throttle so our --sleep cadence is what counts."""
    provider.refresh(force=True)


def _profile_graph(path: str | Path = DEFAULT_GRAPH_JOURNAL) -> dict[str, object]:
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


def _check_persistence_alerts(
    edges_now: list,
    threshold: int,
    already_alerted: set[str],
    alerter: Alerter,
) -> None:
    """Fire ONE alert per (domain) crossing the persistence threshold. De-dup via the caller's set."""
    for dom in _WATCHED_DOMAINS:
        n = edge_persistence(edges_now, predicate="elevated_in", object=("domain", dom))
        key = f"elevated_in::{dom}"
        if n >= threshold and key not in already_alerted:
            title, body = format_persistence(domain=dom, run_length=n, threshold=threshold)
            alerter.send(title, body)
            already_alerted.add(key)
        elif n < threshold and key in already_alerted:
            # Signal dropped back below threshold — clear the dedupe so re-crossings fire again.
            already_alerted.discard(key)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=8,
                    help="How many snapshot fetches to run.")
    ap.add_argument("--sleep", type=float, default=900.0,
                    help="Seconds between snapshots. Vendor cache is ~15 min; anything shorter "
                         "re-writes the same payload against a stale age.")
    ap.add_argument("--wash-min-hours", type=float, default=72.0,
                    help="Minimum hours between temporal-gate wash runs. Time-based so it's "
                         "unaffected by --sleep or --iterations. Default 72h (three days) — the "
                         "gate is meant to be occasional cleanup, not per-poll churn.")
    ap.add_argument("--persistence-threshold", type=int, default=5,
                    help="Emit an alert when an elevated_in run reaches N consecutive polls "
                         "(default 5). Once-per-crossing, not per-poll.")
    ap.add_argument("--alerts/--no-alerts", dest="alerts", default=True,
                    action=argparse.BooleanOptionalAction,
                    help="Send persistence-hit + wash-event notifications to the alerter "
                         "(Telegram + email + stdout). Default ON.")
    args = ap.parse_args()

    provider = _build_overlay_provider(refresh_seconds=args.sleep)
    alerter = _build_alerter() if args.alerts else None
    already_alerted: set[str] = set()
    # Time-based wash cadence — track the last wash's wall-clock, not iteration count.
    last_wash_ts: float | None = None
    wash_min_seconds = args.wash_min_hours * 3600.0

    before = _profile_graph()
    print(f"[thicken] start: {before['edges_total']} edges, {before['nodes_total']} nodes",
          flush=True)
    print(f"[thicken] nodes by type: {before['nodes_by_type']}", flush=True)
    print(f"[thicken] polling via OverlayProvider (canonical live-path poller) "
          f"- overlay decisions computed AND journaled per refresh", flush=True)
    print(f"[thicken] alerts: {'ON' if alerter else 'OFF'} "
          f"(persistence threshold {args.persistence_threshold}, "
          f"wash cadence >= {args.wash_min_hours}h)", flush=True)

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
            # Persistence check — cheap on the reloaded edge list.
            if alerter:
                try:
                    edges_now = load_edges()
                    _check_persistence_alerts(edges_now, args.persistence_threshold,
                                               already_alerted, alerter)
                except Exception as e:
                    print(f"[thicken] persistence check failed: {e}", flush=True)
                # Thesis firing — run interpret() on the exact snapshot the overlay decided on
                # (via provider.last_snapshot, no second fetch), route named theses to Telegram.
                # De-duplicated by thesis name; a thesis that stops firing clears its dedup so a
                # re-fire alerts again.
                try:
                    snap_used = provider.last_snapshot
                    if snap_used is not None:
                        theses = interpret(snap_used)
                        live_keys: set[str] = set()
                        for t in theses:
                            if t.name == "No notable configuration":
                                continue
                            key = f"thesis::{t.name}"
                            live_keys.add(key)
                            if key not in already_alerted:
                                title, body = format_thesis(t, theme_exemplars=THEME_EXEMPLARS)
                                alerter.send(title, body)
                                already_alerted.add(key)
                        # Clear thesis dedup keys for theses that stopped firing so re-crossings alert.
                        stale = {k for k in already_alerted
                                  if k.startswith("thesis::") and k not in live_keys}
                        already_alerted -= stale
                except Exception as e:
                    print(f"[thicken] thesis firing failed: {e}", flush=True)

        # Temporal gate — time-based cadence (default 72h min between runs).
        now = time.time()
        due_for_wash = last_wash_ts is None or (now - last_wash_ts) >= wash_min_seconds
        if due_for_wash:
            try:
                summary = wash_journal_file()
                last_wash_ts = now
                pruned = summary["pruned"]
                pct = (pruned / summary["before"] * 100.0) if summary["before"] else 0.0
                print(f"[thicken] wash: {summary['before']} -> {summary['after']} edges "
                      f"(pruned {pruned}, {pct:.1f}%)", flush=True)
                if alerter and pruned > 0:
                    title, body = format_wash(
                        before=summary["before"], after=summary["after"], pruned=pruned,
                    )
                    alerter.send(title, body)
            except Exception as e:
                print(f"[thicken] wash failed: {e}", flush=True)
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
