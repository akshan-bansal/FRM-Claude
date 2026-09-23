"""Interleave Kraken deep-history pulls with tick-cache catch-up passes, strictly one at a time.

Kraken's public tier allows roughly one request per second per IP, so ``fetch_crypto_history.py``
(multi-year daily bars) and ``deepen_kraken_trades.py`` (tick-level trade rows) must never run
concurrently — overlapping them trips the limiter and both come back short. This driver serializes
them: it waits for any fetch already in flight, then alternates one pair's deep history with one
bounded tick pass across the sleeve, and repeats down the priority order.

Pairs whose ``<WIRE>_daily.parquet`` already exists are skipped, so stopping and restarting never
redoes finished work. ``fetch_crypto_history.py`` writes its parquet only at the end of a pair, so
a hard kill mid-pair loses that pair's progress: stop between steps instead, by creating the
``--stop-file`` sentinel (default ``state/STOP_KRAKEN_FETCH``).

Each tick pass moves every sleeve pair forward by at most ``--tick-pages`` * 1000 trades, so a
cache that is days behind closes its gap over several passes rather than in one.

Usage::

    python scripts/kraken_interleave.py                      # full priority order
    python scripts/kraken_interleave.py --tick-pages 400     # bigger catch-up bites
    python scripts/kraken_interleave.py --pairs ZECUSD,LINKUSD
    touch state/STOP_KRAKEN_FETCH                            # stop between steps
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import UTC, datetime
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
    """False when ``<pair>_daily.parquet`` is already cached — that pair's deep pull is done."""
    return not (cache_dir / f"{pair}_daily.parquet").exists()


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
    ap.add_argument("--history-pages", type=int, default=15000,
                    help="--max-pages cap for one pair's deep-history pull.")
    ap.add_argument("--cache", type=Path, default=REPO / "data" / "cache")
    ap.add_argument("--stop-file", type=Path, default=REPO / "state" / "STOP_KRAKEN_FETCH",
                    help="Create this file to stop cleanly between steps (never mid-pair).")
    args = ap.parse_args()

    pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]
    log(f"{len(pairs)} pair(s) queued; stop cleanly with: touch {args.stop_file}")

    while fetch_running():
        log("another Kraken fetch is running; waiting 60s")
        time.sleep(60)

    for pair in pairs:
        if args.stop_file.exists():
            log("stop file seen; exiting between steps")
            return 0
        if needs_history(pair, args.cache):
            run_step("fetch_crypto_history.py",
                     ["--pair", pair, "--since", "0",
                      "--max-pages", str(args.history_pages), "--sleep", "1.05"],
                     f"history {pair}")
        else:
            log(f"SKIP {pair}: {pair}_daily.parquet already cached")
        if args.stop_file.exists():
            log("stop file seen; exiting between steps")
            return 0
        run_step("deepen_kraken_trades.py",
                 ["--max-pages", str(args.tick_pages), "--sleep", "1.1"],
                 f"tick pass after {pair}")
    log("driver finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
