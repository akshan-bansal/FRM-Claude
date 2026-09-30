"""Interleave Kraken deep-history pulls with tick-cache catch-up passes, strictly one at a time.

Kraken's public tier allows roughly one request per second per IP, so ``fetch_crypto_history.py``
(multi-year daily bars) and ``deepen_kraken_trades.py`` (tick-level trade rows) must never run
concurrently — overlapping them trips the limiter and both come back short. This driver serializes
them: it waits for any fetch already in flight, then alternates one pair's deep history with one
bounded tick pass across the sleeve, and repeats down the priority order.

A pair is skipped once its ``<WIRE>_history.json`` checkpoint reports ``complete``, so stopping and
restarting never redoes finished work, and a partially fetched pair resumes from its cursor rather
than from the earliest trade. Stop cleanly by creating the ``--stop-file`` sentinel (default
``state/STOP_KRAKEN_FETCH``), which is checked between steps; a hard kill now costs at most the
current chunk, since ``fetch_crypto_history.py`` checkpoints every ``--chunk-pages``.

Each tick pass moves every sleeve pair forward by at most ``--tick-pages`` * 1000 trades, so a
cache that is days behind closes its gap over several passes rather than in one. ``--skip-ticks``
runs history only — use it while a paper session is live, since the tick passes are the burstier
load on the shared Kraken rate limit and can starve a session's quotes.

A pair needing more than ``--history-pages`` in one run is re-run until its checkpoint reports
``complete`` (bounded by ``--history-runs-per-pair`` so one stubborn pair can't block the queue).

``--until HH:MM`` (local) gives the driver a hard finish time and is what keeps a scheduled
overnight run from still fetching during market hours: before each run it shortens that run's page
budget to what fits in the time left, and it stops rather than start a run it cannot finish.

Usage::

    python scripts/kraken_interleave.py                      # full priority order
    python scripts/kraken_interleave.py --tick-pages 400     # bigger catch-up bites
    python scripts/kraken_interleave.py --pairs ZECUSD,LINKUSD
    touch state/STOP_KRAKEN_FETCH                            # stop between steps
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
    except Exception:                                                  # pragma: no cover
        pass

REPO = Path(__file__).resolve().parents[1]

# Deep-history priority from NEXT_SESSION.md ("Deep-history fetch for the crypto sleeve"). BTC, ETH
# and PAXG already have daily bars; PAXG left the traded sleeve on 2026-09-23.
DEFAULT_PAIRS = ("XMRUSD", "ZECUSD", "LINKUSD", "XRPUSD", "XLMUSD", "SOLUSD", "ADAUSD", "POLUSD",
                 "UNIUSD", "AAVEUSD")

# Marks a process as one of ours holding the rate limit.
FETCH_MARKERS = ("fetch_crypto_history", "deepen_kraken_trades")


def log(msg: str) -> None:
    print(f"[interleave {datetime.now(UTC):%H:%M:%S}] {msg}", flush=True)


def needs_history(pair: str, cache_dir: Path) -> bool:
    """False only when the pair's checkpoint says ``complete``.

    Deliberately NOT "does the parquet exist": since 2026-09-23 ``fetch_crypto_history.py``
    checkpoints every chunk, so a partial pair has a parquet after its first few minutes. Keying
    on the file would abandon a pair mid-history.
    """
    ck = cache_dir / f"{pair}_history.json"
    if not ck.exists():
        return True
    try:
        return not json.loads(ck.read_text(encoding="utf-8")).get("complete", False)
    except (OSError, ValueError):
        return True                    # unreadable checkpoint: safer to resume than to skip


def parse_deadline(until: str, *, now: datetime | None = None) -> datetime | None:
    """``"HH:MM"`` local -> the next such moment. Empty string means no deadline."""
    if not until.strip():
        return None
    hh, _, mm = until.strip().partition(":")
    target = dtime(int(hh), int(mm or 0))
    now = now or datetime.now()
    today = datetime.combine(now.date(), target)
    return today if today > now else today + timedelta(days=1)


def pages_for_run(cap: int, deadline: datetime | None, *, sleep_s: float,
                  now: datetime | None = None) -> int:
    """Page budget for the next run: the cap, or what fits before ``deadline``, whichever is less."""
    if deadline is None:
        return cap
    remaining_s = (deadline - (now or datetime.now())).total_seconds()
    if remaining_s <= 0:
        return 0
    return max(0, min(cap, int(remaining_s / max(sleep_s, 0.01)) - 30))   # 30-page safety margin


def fetch_running() -> bool:
    """True while a history/deepen fetch is already holding Kraken's rate limit.

    Fails open (returns False) if the process list can't be read: the caller then starts its own
    fetch, which at worst is the pre-existing one-job-at-a-time behaviour of running by hand.
    """
    if sys.platform == "win32":
        pattern = "|".join(FETCH_MARKERS)
        cmd = ["powershell", "-NoProfile", "-Command",
               "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
               f"Where-Object {{ $_.CommandLine -match '{pattern}' }} | Measure-Object).Count"]
    else:                                                              # pragma: no cover — CI is Windows
        cmd = ["pgrep", "-fc", "|".join(FETCH_MARKERS)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:                 # pragma: no cover
        log(f"process check failed ({e}); assuming nothing is running")
        return False
    return (out.stdout.strip() or "0") not in ("0", "")


def run_step(script: str, args: list[str], label: str) -> int:
    log(f"START {label}")
    t0 = time.time()
    rc = subprocess.run([sys.executable, str(REPO / "scripts" / script), *args],
                        cwd=REPO, check=False).returncode
    log(f"END   {label} rc={rc} elapsed={(time.time() - t0) / 60:.1f}min")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default=",".join(DEFAULT_PAIRS),
                    help="Comma-separated Kraken wire pairs, in the order to fetch them.")
    ap.add_argument("--tick-pages", type=int, default=150,
                    help="--max-pages for each tick catch-up pass (applies per sleeve pair).")
    ap.add_argument("--history-pages", type=int, default=7500,
                    help="--max-pages for ONE history run (~2h at 1 req/s). A pair needing more "
                         "is re-run from its checkpoint.")
    ap.add_argument("--history-runs-per-pair", type=int, default=6,
                    help="Cap on resume runs per pair per driver pass, so one pair can't block "
                         "the rest of the queue forever.")
    ap.add_argument("--until", dest="until", default="",
                    help="Local HH:MM to finish by (e.g. 08:45). Each run's page budget is "
                         "trimmed to fit the remaining time; empty = run until the queue is done.")
    ap.add_argument("--min-run-pages", dest="min_run_pages", type=int, default=250,
                    help="Don't start a history run smaller than this near the deadline — one "
                         "chunk is the smallest useful unit of work.")
    ap.add_argument("--skip-ticks", action="store_true",
                    help="History only — no tick catch-up passes (use while a paper session is "
                         "live so it keeps the Kraken rate limit).")
    ap.add_argument("--cache", type=Path, default=REPO / "data" / "cache")
    ap.add_argument("--stop-file", type=Path, default=REPO / "state" / "STOP_KRAKEN_FETCH",
                    help="Create this file to stop cleanly between steps (never mid-pair).")
    args = ap.parse_args()

    pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]
    deadline = parse_deadline(args.until)
    log(f"{len(pairs)} pair(s) queued; stop cleanly with: touch {args.stop_file}"
        + (f"; finishing by {deadline:%Y-%m-%d %H:%M} local" if deadline else ""))

    while fetch_running():
        log("another Kraken fetch is running; waiting 60s")
        time.sleep(60)

    for pair in pairs:
        if args.stop_file.exists():
            log("stop file seen; exiting between steps")
            return 0
        runs = 0
        while needs_history(pair, args.cache) and runs < args.history_runs_per_pair:
            if args.stop_file.exists():
                log("stop file seen; exiting between steps")
                return 0
            budget = pages_for_run(args.history_pages, deadline, sleep_s=1.05)
            if budget < args.min_run_pages:
                log(f"deadline reached (room for {budget} pages); stopping with {pair} at its "
                    f"checkpoint")
                return 0
            runs += 1
            # No --since: the fetch resumes from the pair's own checkpoint.
            run_step("fetch_crypto_history.py",
                     ["--pair", pair, "--max-pages", str(budget), "--sleep", "1.05"],
                     f"history {pair} run {runs}/{args.history_runs_per_pair} ({budget} pages)")
        if not needs_history(pair, args.cache):
            log(f"DONE {pair}: history complete")
        elif runs:
            log(f"PARTIAL {pair}: still incomplete after {runs} run(s); queued for a later pass")
        if args.stop_file.exists():
            log("stop file seen; exiting between steps")
            return 0
        if args.skip_ticks:
            log(f"SKIP tick pass after {pair} (--skip-ticks)")
            continue
        run_step("deepen_kraken_trades.py",
                 ["--max-pages", str(args.tick_pages), "--sleep", "1.1"],
                 f"tick pass after {pair}")
    log("driver finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
