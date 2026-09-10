"""Overnight orchestrator — runs the sequence approved for the 2026-09-04 overnight window.

Chained (not parallel — sequential is safer against Kraken + QT rate-limit contention):
  1. QT cache warm for missing ETF proxies (bonds, precious-metals variants)
  2. WF on all newly-cached + already-cached ETF proxies
  3. FX pairs discovery scan → FX pairs walk-forward on tradeable shortlist
  4. Crypto deep-history fetch (2-3 hrs Kraken pull) → crypto WF on deep bars

Each step logs its own stdout under ``reports/overnight_<date>_step<N>.log``. If a step raises,
the orchestrator prints the exception, moves on to the next step, and exits non-zero at the end
so a monitor treats the overall run as failed even if some steps produced output.

Not called by the runner; intended to be launched once as a long-running background job.
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import date
from pathlib import Path

REPORTS_DIR = Path("reports")
TAG = date.today().isoformat()


# ETF proxy set — the names that would supplement WALK_FORWARD_VALIDATED once WF-cleared. Some
# are already deeply cached (GLD/SLV/USO/DBC/DBA/UNG), some need first-fetch (DIA, IAU, SGOL,
# PSLV), and the bond ETFs (TLT/IEF/SHY already thin; IEI/LQD/HYG/JNK/EMB/MUB/TFI/GOVT/BND/AGG/
# MBB/TLH not cached at all).
_PROXY_ETFS = (
    "DIA", "IAU", "SGOL", "PSLV",           # first-fetch precious metals + Dow
    "TLT", "IEF", "SHY",                     # deep-refetch (currently 185 bars)
    "IEI", "LQD", "HYG", "JNK", "EMB",       # bond first-fetch
    "MUB", "TFI", "GOVT", "BND", "AGG",
    "MBB", "TLH",
    "GLD", "SLV", "USO", "UNG", "DBC", "DBA",   # already deeply cached — cheap re-warm
)


def _run(label: str, cmd: list[str], step_num: int) -> int:
    """Run a subprocess, log to reports/, return exit code. Never raises."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = REPORTS_DIR / f"overnight_{TAG}_step{step_num}_{label}.log"
    print(f"\n[overnight] STEP {step_num}: {label}", flush=True)
    print(f"[overnight]   cmd: {' '.join(cmd)}", flush=True)
    print(f"[overnight]   log: {log_path}", flush=True)
    t0 = time.time()
    try:
        with log_path.open("w", encoding="utf-8") as fh:
            r = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True, check=False)
    except Exception as e:
        print(f"[overnight]   FAILED to spawn: {e}", flush=True)
        return 999
    dur = time.time() - t0
    print(f"[overnight]   done in {dur:.0f}s, exit={r.returncode}", flush=True)
    return r.returncode


def main() -> int:
    print(f"[overnight] starting orchestrator, tag={TAG}", flush=True)
    started = time.time()
    exits: dict[str, int] = {}

    python = sys.executable

    # 1) QT cache warm for the proxy set — 5 years so WF has enough for multi-fold WF
    exits["warm"] = _run(
        "warm_cache",
        [python, "-u", "scripts/warm_cache.py",
         "--symbols", ",".join(_PROXY_ETFS), "--years", "5"],
        step_num=1,
    )

    # 2) WF on the proxy set — reads the cache that step 1 just filled + what was already there
    exits["wf_symbols"] = _run(
        "wf_symbols",
        [python, "-u", "scripts/walk_forward_symbols.py",
         "--symbols", ",".join(_PROXY_ETFS),
         "--tag", f"proxies_{TAG}"],
        step_num=2,
    )

    # 3a) FX pairs discovery — Kraken shallow OHLC per pair + Engle-Granger enumeration
    exits["fx_scan"] = _run(
        "fx_pairs_scan",
        [python, "-u", "scripts/fx_pairs_scan.py",
         "--tag", TAG],
        step_num=3,
    )
    # 3b) FX pairs WF — reads the shortlist from 3a
    exits["fx_wf"] = _run(
        "walk_forward_pairs",
        [python, "-u", "scripts/walk_forward_pairs.py",
         "--tag", TAG,
         "--input", str(REPORTS_DIR / f"fx_pairs_scan_{TAG}.csv")],
        step_num=4,
    )

    # 4a) Crypto deep-history fetch — one pair at a time via /public/Trades pagination
    #     Runs to completion for each pair before moving on; slow but hits Kraken politely.
    from trading_live_claude.analysis.universe import CRYPTO_SLEEVE
    for i, entry in enumerate(CRYPTO_SLEEVE.values(), start=1):
        exits[f"crypto_fetch_{entry.pair}"] = _run(
            f"crypto_fetch_{entry.pair}",
            [python, "-u", "scripts/fetch_crypto_history.py",
             "--pair", entry.pair],
            step_num=5 + i - 1,
        )

    # 4b) Crypto WF on deep bars — uses parquet from 4a, or falls back to shallow if the fetch
    #     didn't complete
    exits["crypto_wf"] = _run(
        "walk_forward_crypto",
        [python, "-u", "scripts/walk_forward_crypto.py"],
        step_num=99,
    )

    dur = time.time() - started
    print(f"\n[overnight] === COMPLETE in {dur:.0f}s ({dur/3600:.1f}h) ===", flush=True)
    for step, code in exits.items():
        print(f"[overnight]   {step:30s} exit={code}", flush=True)
    return 1 if any(c != 0 for c in exits.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
