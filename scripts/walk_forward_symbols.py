"""Walk-forward an arbitrary list of already-cached symbols.

Gap-filler between :mod:`sweep_universe` (walks the whole cache) and :mod:`walk_forward_crypto`
(walks only the fixed :data:`CRYPTO_SLEEVE`). This one takes an explicit ``--symbols`` list and
runs each one through the same :func:`sweep_universe.walk_forward` helper the equity pool uses
— so numbers stay comparable and the same tier rule applies.

Reports to ``reports/wf_symbols_<tag>.{csv,md}``. Does NOT edit ``analysis/universe.py`` — the
markdown output has a ``_wf(...)`` line per survivor ready to paste into ``WALK_FORWARD_VALIDATED``
after a human review.

Run:  python scripts/walk_forward_symbols.py --symbols DIA,GLD,SLV,IAU,SGOL,USO,UNG,DBC,DBA
"""
from __future__ import annotations

import argparse
import glob
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from sweep_universe import walk_forward       # noqa: E402


DEFAULT_CACHE = Path("data/cache")


def _load_cached_daily(sym: str, cache_dir: Path) -> pd.DataFrame | None:
    """Concatenate any {sym}_1d_*.parquet shards from the QT cache into one dedup'd frame."""
    files = sorted(glob.glob(str(cache_dir / f"{sym}_1d_*.parquet")))
    if not files:
        return None
    df = pd.concat([pd.read_parquet(f) for f in files])
    df = df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    return df if not df.empty else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True,
                    help="Comma-separated symbols to WF. Each must have {sym}_1d_*.parquet in the cache.")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--tag", default=date.today().isoformat())
    ap.add_argument("--reports-dir", type=Path, default=Path("reports"))
    args = ap.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    rows: list[dict] = []
    for sym in syms:
        df = _load_cached_daily(sym, args.cache)
        if df is None:
            print(f"  {sym:8s} no cache -> SKIP", flush=True)
            continue
        if len(df) < 630:
            print(f"  {sym:8s} only {len(df)} bars, need >=630 for one fold -> SKIP", flush=True)
            continue
        wf = walk_forward(df, sym)
        if wf is None:
            print(f"  {sym:8s} no WF result -> SKIP", flush=True)
            continue
        tier = ("robust" if (wf["wfe"] >= 0.5 and wf["oos_score"] > 0
                             and wf["oos_trades"] >= 10) else "watch")
        rows.append({"symbol": sym, "bars": len(df), **wf, "tier": tier})
        print(f"  {sym:8s} bars={len(df)} OOS {wf['oos_score']:.2f} WFE {wf['wfe']:.2f} "
              f"trades {wf['oos_trades']} -> {tier}", flush=True)

    if not rows:
        print("[wf-symbols] no WF results produced.", flush=True)
        return

    frame = pd.DataFrame(rows).sort_values("oos_score", ascending=False)
    args.reports_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.reports_dir / f"wf_symbols_{args.tag}.csv"
    md_path = args.reports_dir / f"wf_symbols_{args.tag}.md"
    frame.to_csv(csv_path, index=False)

    robust = frame[frame.tier == "robust"]
    lines = [f"# WF-symbols run — {args.tag}", ""]
    lines.append(f"Walked-forward {len(frame)} names. {len(robust)} cleared robust "
                  f"(WFE >= 0.5, OOS > 0, >= 10 trades).")
    lines.append("")
    lines.append("| symbol | bars | strategy | params | OOS | IS | WFE | trades | WR | ret | maxDD | tier |")
    lines.append("|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for _, r in frame.iterrows():
        lines.append(
            f"| {r.symbol} | {int(r.bars)} | {r.get('strategy','?')} | "
            f"{r.get('params','?')} | {r.oos_score:.2f} | {r.get('is_score', 0):.2f} | "
            f"{r.wfe:.2f} | {int(r.oos_trades)} | "
            f"{r.get('oos_win_rate', 0):.2f} | {r.get('oos_return', 0):.2%} | "
            f"{r.get('oos_max_drawdown', 0):.2%} | {r.tier} |"
        )
    lines.append("")
    if not robust.empty:
        lines.append("## Ready-to-paste `_wf(...)` entries for WALK_FORWARD_VALIDATED")
        lines.append("```python")
        for _, r in robust.iterrows():
            lines.append(
                f'    "{r.symbol}": _wf("{r.symbol}", "{r.get("strategy","?")}", '
                f'{r.get("params","{}")}, {r.oos_score:.2f}, {r.wfe:.2f}, '
                f'{r.get("oos_return", 0):.4f}, {r.get("oos_max_drawdown", 0):.4f}, '
                f'{int(r.oos_trades)}, "robust", oos_win_rate={r.get("oos_win_rate", 0):.4f}),'
            )
        lines.append("```")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[wf-symbols] wrote {csv_path} and {md_path}  ({len(robust)} robust of {len(frame)})",
          flush=True)


if __name__ == "__main__":
    main()
