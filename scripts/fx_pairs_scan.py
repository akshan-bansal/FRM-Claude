"""FX pair-trading discovery MVP over Kraken's fiat FX pairs.

Fetches shallow (~720-bar) daily OHLC for each of Kraken's fiat FX pairs, then runs
:func:`~trading_live_claude.analysis.pairs.enumerate_pairs` — Engle-Granger cointegration in both
orientations, keeping the more significant direction — across every 2-pair combination. Reports
the tradeable shortlist (cointegrated with a finite half-life) to
``reports/fx_pairs_scan.{csv,md}``.

This is the discovery step for NEXT_SESSION.md item 8 (FX pair-trading). It does NOT wire
``pairs.py`` into live trading and does NOT flip any tier — the intent is that a human reads the
shortlist and decides whether to promote a pair for backtesting. Automated promotion would silently
bind the sleeve to whatever the last Kraken pull said, and that's a change big enough to want on a
human's diff.

FX-specific tuning vs. equity pair defaults:
  * ``max_half_life`` defaults to 60 bars (~3 mo) not 252 — FX pairs mean-revert faster than
    equity pairs when they mean-revert at all; a 6-mo half-life on FX is usually noise, not signal.
  * ``alpha`` stays at 0.05. FX has more autocorrelation than equity so pushing alpha stricter
    doesn't buy much and drops honest pairs.

Run:  python scripts/fx_pairs_scan.py [--pairs EUR/USD,GBP/USD,...] [--min-obs 500] [--tag ...]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from trading_live_claude.analysis.pairs import enumerate_pairs
from trading_live_claude.data.kraken_ohlc import kraken_ohlc

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
    except Exception:
        pass


# Kraken's most-liquid fiat FX pairs. Kept small on purpose — C(n,2) grows quadratically and a full
# ~25-pair enumeration would over-mine on the shallow 720-bar window without any real coverage
# gain. Add more here as tradeable pairs emerge from repeated runs.
DEFAULT_FX_PAIRS = (
    "EUR/USD", "GBP/USD", "USD/CAD", "USD/JPY", "AUD/USD",
    "EUR/GBP", "EUR/JPY", "EUR/CAD", "EUR/CHF", "GBP/JPY",
)


def _fetch_pair(pair: str) -> pd.DataFrame | None:
    """Fetch shallow daily OHLC for one FX pair via Kraken /public/OHLC."""
    try:
        df = kraken_ohlc(pair, interval=1440)      # 1440-minute bars = daily
    except Exception as e:
        print(f"  {pair}: fetch failed ({type(e).__name__}: {e})", flush=True)
        return None
    if df is None or df.empty:
        print(f"  {pair}: empty response", flush=True)
        return None
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default=",".join(DEFAULT_FX_PAIRS),
                    help="Comma-separated Kraken-format FX pairs to scan. Default is the "
                         "most-liquid fiat set.")
    ap.add_argument("--alpha", type=float, default=0.05,
                    help="Cointegration p-value threshold for the tradeable flag.")
    ap.add_argument("--max-half-life", type=float, default=60.0,
                    help="Max half-life (days) for the tradeable flag. FX mean-reverts faster than "
                         "equity — 60d default vs 252d for the equity default.")
    ap.add_argument("--min-obs", type=int, default=500,
                    help="Minimum overlapping bars per pair after inner-join. Kraken /public/OHLC "
                         "caps at ~720 daily bars, so 500 leaves a genuine 220-bar buffer.")
    ap.add_argument("--tag", default=date.today().isoformat(),
                    help="Report suffix. Default: today's ISO date.")
    ap.add_argument("--reports-dir", default="reports")
    args = ap.parse_args()

    pair_list = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]
    reports_dir = Path(args.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)

    print(f"[fx-pairs] fetching {len(pair_list)} FX pairs via Kraken /public/OHLC...", flush=True)
    dfs: dict[str, pd.DataFrame] = {}
    for p in pair_list:
        df = _fetch_pair(p)
        if df is not None and len(df) >= args.min_obs:
            dfs[p] = df
            print(f"  {p:>10}: {len(df)} bars", flush=True)
        elif df is not None:
            print(f"  {p:>10}: {len(df)} bars — below min-obs {args.min_obs}, SKIP", flush=True)

    if len(dfs) < 2:
        raise SystemExit("[fx-pairs] fewer than 2 pairs had usable history — nothing to enumerate.")

    print(f"[fx-pairs] enumerating C({len(dfs)},2) = {len(dfs)*(len(dfs)-1)//2} pair "
          f"combinations at alpha={args.alpha}, max_half_life={args.max_half_life}d",
          flush=True)
    cands = enumerate_pairs(dfs, alpha=args.alpha, max_half_life=args.max_half_life,
                              min_obs=args.min_obs)

    rows = [{
        "sym_y": c.sym_y, "sym_x": c.sym_x, "pvalue": round(c.pvalue, 5),
        "adf_stat": round(c.adf_stat, 3), "hedge_ratio": round(c.hedge_ratio, 4),
        "half_life": round(c.half_life, 2), "n_obs": c.n_obs,
        "cointegrated": c.cointegrated, "tradeable": c.tradeable,
    } for c in cands]
    frame = pd.DataFrame(rows)
    csv_path = reports_dir / f"fx_pairs_scan_{args.tag}.csv"
    md_path = reports_dir / f"fx_pairs_scan_{args.tag}.md"
    frame.to_csv(csv_path, index=False)

    tradeable = frame[frame.tradeable].sort_values("pvalue")
    lines = [f"# FX pair-trading scan — {args.tag}", ""]
    lines.append(f"Enumerated {len(cands)} pair combinations across {len(dfs)} FX pairs. "
                  f"{len(tradeable)} cointegrated + tradeable at alpha={args.alpha} and "
                  f"half-life <= {args.max_half_life}d.")
    lines.append("")
    if not tradeable.empty:
        lines.append("## Tradeable shortlist (ranked by ascending p-value)")
        lines.append("| y | x | p-value | ADF | hedge | half-life (d) | n_obs |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        for _, r in tradeable.iterrows():
            lines.append(f"| {r.sym_y} | {r.sym_x} | {r.pvalue:.4f} | {r.adf_stat:.2f} | "
                          f"{r.hedge_ratio:.3f} | {r.half_life:.1f} | {int(r.n_obs)} |")
        lines.append("")
        lines.append("Next step: pick one or two of these and run a walk-forward via "
                      "`walk_forward_pairs.py` (not yet built) or an ad-hoc pairs.py backtest. "
                      "Do NOT promote to live before WF clears — pair-trading is one of the most "
                      "overfit-prone strategy families.")
    else:
        lines.append("## No cointegrated pairs in this window")
        lines.append("Consider widening the pair set, extending the history, or relaxing "
                      "`--max-half-life`. Cointegration is regime-dependent — a scan clean today "
                      "may find pairs next week.")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[fx-pairs] wrote {csv_path} and {md_path}  "
          f"({len(tradeable)} tradeable of {len(cands)} enumerated)", flush=True)


if __name__ == "__main__":
    main()
