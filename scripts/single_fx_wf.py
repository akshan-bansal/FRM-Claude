"""Walk-forward each FX pair as a single-instrument directional strategy.

Complement to ``walk_forward_pairs.py`` — that script scores co-integrated PAIRS-of-pairs
(spread reversion). This one scores each FX pair on its own directional character (trend /
mean-revert / breakout via the same grid the equity sweep uses), against the FX protocol
(504 train / 126 test / step 126, 260-day annualization).

Fetches shallow (~720-bar) Kraken daily OHLC per pair — the same data source
``walk_forward_pairs.py`` uses — so the two runs can be compared directly. Reports survivors to
``reports/single_fx_wf_<tag>.{csv,md}`` with the paste-ready ``_wf(...)`` lines.

Run:  python scripts/single_fx_wf.py [--pairs EUR/USD,GBP/USD,...] [--tag ...]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from sweep_universe import walk_forward       # noqa: E402

from trading_live_claude.data.kraken_ohlc import kraken_ohlc


# Same default set as fx_pairs_scan.py so the two are directly comparable.
DEFAULT_FX_PAIRS = (
    "EUR/USD", "GBP/USD", "USD/CAD", "USD/JPY", "AUD/USD",
    "EUR/GBP", "EUR/JPY", "EUR/CAD", "EUR/CHF",
)


def _fetch_pair(pair: str) -> pd.DataFrame | None:
    try:
        df = kraken_ohlc(pair, interval=1440)
    except Exception as e:
        print(f"  {pair}: fetch failed ({type(e).__name__}: {e})", flush=True)
        return None
    return df if df is not None and not df.empty else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default=",".join(DEFAULT_FX_PAIRS),
                    help="Comma-separated Kraken-format FX pairs to WF.")
    ap.add_argument("--tag", default=date.today().isoformat())
    ap.add_argument("--reports-dir", type=Path, default=Path("reports"))
    ap.add_argument("--min-bars", type=int, default=500)
    args = ap.parse_args()

    pair_list = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]
    print(f"[fx-single] WF {len(pair_list)} FX pairs against the FX protocol", flush=True)

    rows: list[dict] = []
    for pair in pair_list:
        df = _fetch_pair(pair)
        if df is None or len(df) < args.min_bars:
            print(f"  {pair:>10}: {0 if df is None else len(df)} bars < min-obs {args.min_bars}, SKIP",
                  flush=True)
            continue
        # walk_forward classifies via `intel.routing.classify_symbol` if asset_class not passed;
        # FX pairs with "/" get routed to "crypto" (BASE/QUOTE heuristic). Force fx here.
        wf = walk_forward(df, pair, asset_class="fx")
        if wf is None:
            print(f"  {pair:>10}: no WF result", flush=True)
            continue
        tier = ("robust" if (wf["wfe"] >= 0.5 and wf["oos_score"] > 0
                             and wf["oos_trades"] >= 10) else "watch")
        rows.append({"pair": pair, "bars": len(df), **wf, "tier": tier})
        print(f"  {pair:>10}: OOS {wf['oos_score']:.2f} WFE {wf['wfe']:.2f} "
              f"trades {wf['oos_trades']} -> {tier}", flush=True)

    if not rows:
        print("[fx-single] no WF results produced.", flush=True)
        return

    frame = pd.DataFrame(rows).sort_values("oos_score", ascending=False)
    args.reports_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.reports_dir / f"single_fx_wf_{args.tag}.csv"
    md_path = args.reports_dir / f"single_fx_wf_{args.tag}.md"
    frame.to_csv(csv_path, index=False)

    robust = frame[frame.tier == "robust"]
    lines = [f"# Single-FX directional WF — {args.tag}", ""]
    lines.append(f"Walked-forward {len(frame)} FX pairs against the FX protocol "
                  f"(train=504 / test=126 / step=126). "
                  f"{len(robust)} cleared robust (WFE >= 0.5, OOS > 0, >= 10 trades).")
    lines.append("")
    lines.append("| pair | bars | strategy | params | OOS | IS | WFE | trades | WR | ret | maxDD | tier |")
    lines.append("|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for _, r in frame.iterrows():
        lines.append(
            f"| {r.pair} | {int(r.bars)} | {r.get('strategy','?')} | "
            f"{r.get('params','?')} | {r.oos_score:.2f} | {r.get('is_score', 0):.2f} | "
            f"{r.wfe:.2f} | {int(r.oos_trades)} | "
            f"{r.get('oos_win_rate', 0):.2f} | {r.get('oos_return', 0):.2%} | "
            f"{r.get('oos_max_drawdown', 0):.2%} | {r.tier} |"
        )
    lines.append("")
    if not robust.empty:
        lines.append("## Ready-to-paste `_wf(...)` entries")
        lines.append("```python")
        for _, r in robust.iterrows():
            lines.append(
                f'    "{r.pair}": _wf("{r.pair}", "{r.get("strategy","?")}", '
                f'{r.get("params","{}")}, {r.oos_score:.2f}, {r.wfe:.2f}, '
                f'{r.get("oos_return", 0):.4f}, {r.get("oos_max_drawdown", 0):.4f}, '
                f'{int(r.oos_trades)}, "robust", asset_class="fx", '
                f'oos_win_rate={r.get("oos_win_rate", 0):.4f}),'
            )
        lines.append("```")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[fx-single] wrote {csv_path} and {md_path}  ({len(robust)} robust of {len(frame)})",
          flush=True)


if __name__ == "__main__":
    main()
