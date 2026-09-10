"""Targeted-basket orchestrator — successor to overnight_run.py.

Same chained-subprocess pattern as overnight_run.py, but scoped to the current selection:

  * Targeted FX legs — 4 EUR-cross legs feeding the two short-half-life cointegrated pairs
    (EUR/GBP~EUR/CAD, EUR/CHF~EUR/JPY). USD-cross legs deliberately excluded per user selection.
  * CRYPTO_SLEEVE deep-fetch — all 7 pairs, using the patched (7-column-tolerant) kraken_ohlc.
  * Chained WF passes after each fetch batch: targeted FX pairs WF against deep parquets, then
    walk_forward_crypto with --no-shallow-fallback to score against the deep bars.

Design mirrors overnight_run.py (subprocess.run with per-step log files under reports/),
so the failure mode of one step doesn't kill the whole chain — the orchestrator prints the
exception, moves on, and exits non-zero if any step failed. That matches the "wake up tomorrow"
horizon this is designed for.

Not launched by default; intended for you to run once ("python scripts/targeted_orchestrator.py"),
which becomes the single background task you can monitor from a single log file.
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import date
from pathlib import Path

REPORTS_DIR = Path("reports")
TAG = date.today().isoformat()

# 4 EUR-cross legs supporting the two short-half-life pairs the user selected 2026-09-04.
# USD-cross legs deliberately omitted; add them back if a full top-10 cointegrated FX WF is
# wanted (see NEXT_SESSION.md).
_FX_LEGS = ("EURGBP", "EURCAD", "EURCHF", "EURJPY")

# The two cointegrated pairs the WF should score against deep bars.
_FX_TARGETED_PAIRS = [
    {"sym_y": "EUR/GBP", "sym_x": "EUR/CAD", "pvalue": 0.0009, "adf_stat": -4.11,
     "hedge_ratio": 0.507, "half_life": 9.2, "n_obs": 721, "cointegrated": True, "tradeable": True},
    {"sym_y": "EUR/CHF", "sym_x": "EUR/JPY", "pvalue": 0.0017, "adf_stat": -3.95,
     "hedge_ratio": -0.136, "half_life": 9.3, "n_obs": 721, "cointegrated": True, "tradeable": True},
]

# CRYPTO_SLEEVE — resolved from the universe module rather than hardcoded so a pool change
# doesn't silently skip a pair.


def _run(label: str, cmd: list[str], step_num: int) -> int:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = REPORTS_DIR / f"targeted_{TAG}_step{step_num}_{label}.log"
    print(f"\n[targeted] STEP {step_num}: {label}", flush=True)
    print(f"[targeted]   cmd: {' '.join(cmd)}", flush=True)
    print(f"[targeted]   log: {log_path}", flush=True)
    t0 = time.time()
    try:
        with log_path.open("w", encoding="utf-8") as fh:
            r = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True, check=False)
    except Exception as e:
        print(f"[targeted]   FAILED to spawn: {e}", flush=True)
        return 999
    print(f"[targeted]   done in {time.time()-t0:.0f}s, exit={r.returncode}", flush=True)
    return r.returncode


def main() -> int:
    print(f"[targeted] starting orchestrator, tag={TAG}", flush=True)
    t_start = time.time()
    exits: dict[str, int] = {}

    python = sys.executable

    # 1) FX deep-fetches — 4 EUR legs, sequential
    for i, leg in enumerate(_FX_LEGS, start=1):
        exits[f"fx_fetch_{leg}"] = _run(
            f"fx_fetch_{leg}",
            [python, "-u", "scripts/fetch_crypto_history.py", "--pair", leg],
            step_num=i,
        )

    # 2) Build the targeted-pairs scan CSV so walk_forward_pairs has an input
    import json
    scan_csv = REPORTS_DIR / f"fx_pairs_scan_targeted_{TAG}.csv"
    with scan_csv.open("w", encoding="utf-8") as fh:
        fh.write("sym_y,sym_x,pvalue,adf_stat,hedge_ratio,half_life,n_obs,cointegrated,tradeable\n")
        for r in _FX_TARGETED_PAIRS:
            fh.write(f"{r['sym_y']},{r['sym_x']},{r['pvalue']},{r['adf_stat']},"
                      f"{r['hedge_ratio']},{r['half_life']},{r['n_obs']},"
                      f"{r['cointegrated']},{r['tradeable']}\n")
    print(f"[targeted] wrote {scan_csv}", flush=True)

    # 3) Targeted FX pairs WF against deep bars
    exits["fx_wf_targeted"] = _run(
        "fx_wf_targeted",
        [python, "-u", "scripts/walk_forward_pairs.py",
         "--tag", f"targeted_{TAG}", "--input", str(scan_csv),
         "--top", "2", "--min-obs", "800"],
        step_num=len(_FX_LEGS) + 1,
    )

    # 4) Crypto sleeve deep-fetches — 7 pairs, sequential
    from trading_live_claude.analysis.universe import CRYPTO_SLEEVE
    for i, entry in enumerate(CRYPTO_SLEEVE.values(), start=1):
        exits[f"crypto_fetch_{entry.pair}"] = _run(
            f"crypto_fetch_{entry.pair}",
            [python, "-u", "scripts/fetch_crypto_history.py", "--pair", entry.pair],
            step_num=len(_FX_LEGS) + 1 + i,
        )

    # 5) Crypto WF on deep parquets — no shallow fallback so the run is honest about
    #    which pairs actually got deep data
    exits["crypto_wf"] = _run(
        "crypto_wf",
        [python, "-u", "scripts/walk_forward_crypto.py", "--no-shallow-fallback"],
        step_num=99,
    )

    dur = time.time() - t_start
    print(f"\n[targeted] === COMPLETE in {dur:.0f}s ({dur/3600:.1f}h) ===", flush=True)
    for step, code in exits.items():
        print(f"[targeted]   {step:30s} exit={code}", flush=True)
    return 1 if any(c != 0 for c in exits.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
