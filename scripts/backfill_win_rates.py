"""Backfill ``oos_win_rate`` on every WALK_FORWARD_VALIDATED entry.

Reruns ``walk_forward`` on the 32 validated names only — skipping the 456-symbol in-sample
panel that the full ``sweep_universe.py`` run wastes time on when we already know which
symbols we want scored. Uses the same helper (per-class WF_PROTOCOLS, cost model, etc.) so
the numbers are consistent with a full resweep — this is exactly the same walk-forward, run
on a curated subset.

Output:
- ``reports/backfill_win_rates.csv`` — sym, oos_win_rate, oos_score, wfe, oos_trades, tier
- Optional patch of ``src/trading_live_claude/analysis/universe.py`` (--patch) so each _wf()
  call gains its win rate. The patch is DISABLED by default; a human should read the CSV
  first, then rerun with --patch when the numbers look right.

Cadence guess: ~30 minutes total (32 symbols × ~1 min each based on the last sweep timing).
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

# Import the sweep's walk_forward helper — same per-class protocols and cost model.
sys.path.insert(0, str(Path(__file__).parent))
from sweep_universe import deepest_frames, walk_forward           # noqa: E402

from trading_live_claude.analysis.universe import WALK_FORWARD_VALIDATED       # noqa: E402


REPORT = Path("reports/backfill_win_rates.csv")
UNIVERSE_PATH = Path("src/trading_live_claude/analysis/universe.py")


def _cache_key(sym: str) -> str:
    return sym.replace(".", "_").replace("/", "_")


def _patch_universe(win_rates: dict[str, float]) -> int:
    """Add ``oos_win_rate=<value>`` to every _wf() call in universe.py that matches a key.

    Idempotent: skips lines that already carry ``oos_win_rate=``. Returns the count of edits.
    """
    text = UNIVERSE_PATH.read_text(encoding="utf-8")
    edits = 0
    for sym, wr in win_rates.items():
        # Match the _wf( call for this symbol: "SYM": _wf("SYM", ...)  through the closing paren.
        # Multi-line safe because the source formats each entry on a single line (long).
        pattern = re.compile(
            rf'("{re.escape(sym)}":\s*_wf\("{re.escape(sym)}",[^)]*?)\)',
            re.DOTALL,
        )
        m = pattern.search(text)
        if m is None:
            print(f"[patch] SKIP {sym}: no _wf() call found")
            continue
        call_body = m.group(1)
        if "oos_win_rate" in call_body:
            print(f"[patch] SKIP {sym}: already has oos_win_rate")
            continue
        new_call = f"{call_body}, oos_win_rate={wr:.4f})"
        text = text.replace(m.group(0), new_call, 1)
        edits += 1
    if edits:
        UNIVERSE_PATH.write_text(text, encoding="utf-8")
    return edits


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-bars", type=int, default=400,
                    help="Lower bar so short-history validated names still get scored.")
    ap.add_argument("--patch", action="store_true",
                    help="Rewrite analysis/universe.py with the win rates. OFF by default.")
    args = ap.parse_args()

    print(f"[backfill] loading cache (>= {args.min_bars} bars)...", flush=True)
    frames = deepest_frames(args.min_bars)

    rows: list[dict[str, object]] = []
    win_rates: dict[str, float] = {}
    print(f"[backfill] walk-forward on {len(WALK_FORWARD_VALIDATED)} validated names",
          flush=True)
    for i, (sym, entry) in enumerate(WALK_FORWARD_VALIDATED.items(), 1):
        key = _cache_key(sym)
        df = frames.get(key)
        if df is None:
            df = frames.get(sym)
        if df is None:
            print(f"  [{i}/{len(WALK_FORWARD_VALIDATED)}] {sym}: SKIP (no cache)",
                  flush=True)
            continue
        wf = walk_forward(df, sym, asset_class=entry.asset_class)
        if wf is None:
            print(f"  [{i}/{len(WALK_FORWARD_VALIDATED)}] {sym}: SKIP (WF empty)",
                  flush=True)
            continue
        wr = float(wf.get("oos_win_rate", 0.0))
        win_rates[sym] = wr
        rows.append({
            "sym": sym, "oos_win_rate": wr,
            "oos_score": wf["oos_score"], "wfe": wf["wfe"],
            "oos_trades": wf["oos_trades"], "tier": entry.tier,
            "asset_class": entry.asset_class,
        })
        print(f"  [{i}/{len(WALK_FORWARD_VALIDATED)}] {sym}: "
              f"win_rate {wr * 100:.1f}%  ·  OOS {wf['oos_score']:.2f}  ·  "
              f"trades {wf['oos_trades']}", flush=True)

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with REPORT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["sym", "oos_win_rate", "oos_score",
                                            "wfe", "oos_trades", "tier", "asset_class"])
        w.writeheader()
        w.writerows(rows)
    print(f"\n[backfill] wrote {REPORT} ({len(rows)} rows)", flush=True)

    if args.patch:
        # Do NOT patch entries where the current run produced too few trades — a 0% win rate on
        # 0 (or 1-2) trades is fabricated, not observed, and would overwrite valid history with
        # a numerical lie. 5 trades is the honest minimum for a rate to mean anything.
        good = {r["sym"]: float(r["oos_win_rate"]) for r in rows
                if int(r["oos_trades"]) >= 5}
        skipped = [r["sym"] for r in rows if int(r["oos_trades"]) < 5]
        edits = _patch_universe(good)
        print(f"[backfill] patched {UNIVERSE_PATH} - {edits} _wf() calls updated", flush=True)
        if skipped:
            print(f"[backfill] SKIPPED {len(skipped)} names with <5 trades (win rate not "
                  f"observable in current window): {', '.join(skipped)}", flush=True)
    else:
        print("[backfill] --patch NOT set. Review the CSV first, then rerun with --patch "
              "to write the values into universe.py.", flush=True)


if __name__ == "__main__":
    main()
