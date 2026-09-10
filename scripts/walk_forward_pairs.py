"""Walk-forward the shortlist emitted by ``fx_pairs_scan.py``.

The scan finds cointegrated pairs on the current window. Cointegration is regime-dependent, so a
pair that clears the p<0.05 bar today may not survive walk-forward — a proper WF re-tests each
train fold and scores only the following block out-of-sample. This wrapper does exactly that
against the FX protocol (train=504 / test=126 / step=126) using :class:`PairsZScore`, grid-
searching (window, entry_z, exit_z) on every train fold.

Reads ``reports/fx_pairs_scan_<tag>.csv`` (or ``--input``); filters to ``tradeable`` rows; fetches
both legs' daily OHLC via ``kraken_ohlc``; builds the joined ``close`` + ``close_b`` frame; walks
forward; reports ``reports/fx_pairs_wf_<tag>.{csv,md}``.

Tier decision matches the equity sweep: ``robust`` requires WFE >= 0.5 AND positive OOS score AND
>= 10 OOS trades. Anything else is ``watch``. Never auto-promotes — the intent is that a human
reads the report and decides whether a pair earns a live sleeve.

Run:  python scripts/walk_forward_pairs.py [--input reports/fx_pairs_scan_2026-09-04.csv]
                                            [--top 5] [--tag ...]
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

from trading_live_claude.analysis.pairs import pair_frame
from trading_live_claude.analysis.universe import wf_protocol
from trading_live_claude.backtest import BacktestEngine
from trading_live_claude.backtest.costs import CostModel
from trading_live_claude.data.kraken_ohlc import kraken_ohlc

_DEEP_CACHE_DIR = Path("data/cache")
from trading_live_claude.scoring.objective import ObjectiveAdapter, ObjectiveInput

_OBJ = ObjectiveAdapter.from_name("sortino_over_dd")
from trading_live_claude.strategies import STRATEGIES

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
    except Exception:
        pass


# Pairs-specific parameter grid. Third widening 2026-09-04 — added FX-oriented sub-1σ
# thresholds (entry_z 0.1 - 0.75) after the deep-history pass on EUR crosses showed both
# targeted pairs produced 0-1 OOS trades even with entry_z=1.2. The user's insight: FX EUR-cross
# pairs with 9-10-day half-lives mean-revert TOO FAST for standard z-score gates to capture
# excursions — the spread never sustains a 1σ+ deviation on daily bars. FX-scale thresholds
# (entry_z 0.1 - 0.75, exit_z 0.05 - 0.5) are the honest way to test whether pair-trading works
# at all on daily FX. Equity-scale thresholds retained so the same script still WFs equity pairs.
# Grid: 5 windows × 8 entry × 6 exit = 240 combos per fold. Per-pair WF ~ 40 sec.
#   window: 20 (fast reversion) → 180 (slow-drift FX-suitable)
#   entry_z: 0.1 (FX ultra-tight, 0.1×σ) → 2.5 (equity standard)
#   exit_z: 0.05 (FX tight-exit) → 1.0 (equity partial-reversion)
_PAIRS_GRID = {
    "window": [20, 45, 60, 120, 180],
    "entry_z": [0.1, 0.25, 0.5, 0.75, 1.0, 1.2, 1.5, 2.0, 2.5],
    "exit_z":  [0.05, 0.1, 0.25, 0.5, 0.75, 1.0],
}


@dataclass
class _WFResult:
    sym_y: str
    sym_x: str
    n_folds: int
    oos_score: float
    is_score: float
    wfe: float
    oos_trades: int
    oos_win_rate: float
    oos_return: float
    oos_max_dd: float
    best_params: dict


def _param_combos(grid: dict) -> list[dict]:
    keys = list(grid)
    return [dict(zip(keys, vals)) for vals in product(*grid.values())]


def _score_pairs_fold(eng: BacktestEngine, params: dict, df: pd.DataFrame, sym: str):
    """Run one PairsZScore backtest on a pair-frame fold; return (objective_score, metrics)."""
    try:
        res = eng.run(STRATEGIES["pairs"](**params), df, sym)
    except Exception:
        return None
    m = res.metrics
    return _OBJ.score(ObjectiveInput.from_metrics(m)), m


def _walk_forward_pair(pair_df: pd.DataFrame, sym_y: str, sym_x: str) -> _WFResult | None:
    """Walk-forward one pair frame using the FX protocol."""
    protocol = wf_protocol("fx")
    train_bars, test_bars, step_bars = protocol.train_bars, protocol.test_bars, protocol.step_bars
    n = len(pair_df)
    if n < train_bars + test_bars:
        return None

    eng = BacktestEngine(cost_model=CostModel.from_price(float(pair_df["close"].iloc[-1]),
                                                          is_etf=True))
    label = f"{sym_y}~{sym_x}"
    oos_scores: list[float] = []
    is_scores: list[float] = []
    rets: list[float] = []
    dds: list[float] = []
    fold_wins = 0.0
    trades = 0
    best_params_last: dict = {}

    start = 0
    while start + train_bars + test_bars <= n:
        tr = pair_df.iloc[start:start + train_bars].reset_index(drop=True)
        te = pair_df.iloc[start + train_bars:start + train_bars + test_bars].reset_index(drop=True)
        bt: tuple[float, dict] | None = None
        for p in _param_combos(_PAIRS_GRID):
            got = _score_pairs_fold(eng, p, tr, label)
            if got and (bt is None or got[0] > bt[0]):
                bt = (got[0], p)
        if bt is not None:
            got = _score_pairs_fold(eng, bt[1], te, label)
            if got is not None:
                oos_scores.append(got[0])
                is_scores.append(bt[0])
                rets.append(got[1].total_return)
                dds.append(got[1].max_drawdown)
                trades += got[1].num_trades
                fold_wins += float(got[1].win_rate) * got[1].num_trades
                best_params_last = bt[1]
        start += step_bars

    if not oos_scores:
        return None
    oos = float(np.mean(oos_scores))
    ins = float(np.mean(is_scores))
    return _WFResult(
        sym_y=sym_y, sym_x=sym_x, n_folds=len(oos_scores),
        oos_score=oos, is_score=ins, wfe=(oos / ins if ins > 0 else 0.0),
        oos_trades=trades,
        oos_win_rate=(fold_wins / trades) if trades > 0 else 0.0,
        oos_return=float(np.mean(rets)), oos_max_dd=float(np.mean(dds)),
        best_params=best_params_last,
    )


def _find_latest_scan(reports_dir: Path) -> Path | None:
    matches = sorted(reports_dir.glob("fx_pairs_scan_*.csv"))
    return matches[-1] if matches else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, default=None,
                    help="fx_pairs_scan_*.csv to read the shortlist from. Default: latest in "
                         "--reports-dir.")
    ap.add_argument("--top", type=int, default=10,
                    help="Cap on how many tradeable pairs to WF (ordered by p-value ascending). "
                         "Each pair takes ~15s of grid-search time so a big shortlist can add up.")
    ap.add_argument("--tag", default=date.today().isoformat())
    ap.add_argument("--reports-dir", type=Path, default=Path("reports"))
    ap.add_argument("--min-obs", type=int, default=500)
    args = ap.parse_args()

    input_path = args.input or _find_latest_scan(args.reports_dir)
    if input_path is None or not input_path.exists():
        raise SystemExit(f"[fx WF] no fx_pairs_scan_*.csv found in {args.reports_dir}. "
                          f"Run scripts/fx_pairs_scan.py first.")
    print(f"[fx WF] reading shortlist from {input_path}", flush=True)

    scan = pd.read_csv(input_path)
    tradeable = scan[scan.tradeable].sort_values("pvalue").head(args.top)
    if tradeable.empty:
        print("[fx WF] shortlist has no tradeable pairs — nothing to walk-forward.", flush=True)
        return
    print(f"[fx WF] WF'ing {len(tradeable)} pairs against FX protocol "
          f"(train={wf_protocol('fx').train_bars}, test={wf_protocol('fx').test_bars})",
          flush=True)

    # Pre-fetch each unique leg once — a pair-heavy shortlist reuses the same legs many times.
    unique_syms = sorted(set(tradeable.sym_y) | set(tradeable.sym_x))
    leg_dfs: dict[str, pd.DataFrame] = {}
    for sym in unique_syms:
        try:
            # Prefer the deep parquet (kraken_ohlc_deep output) when it exists — vastly more
            # bars than the shallow OHLC endpoint's 720-cap. The pair strings written by
            # fetch_crypto_history strip separators (EUR/GBP -> EURGBP), so try that first.
            deep_stem = sym.replace("/", "").replace("-", "").upper()
            deep_path = _DEEP_CACHE_DIR / f"{deep_stem}_daily.parquet"
            if deep_path.exists():
                import pandas as _pd
                df = _pd.read_parquet(deep_path)
                print(f"  {sym}: loaded {len(df)} bars from deep cache ({deep_path.name})",
                      flush=True)
            else:
                df = kraken_ohlc(sym, interval=1440)
        except Exception as e:
            print(f"  {sym}: fetch failed ({type(e).__name__}: {e})", flush=True)
            continue
        if df is None or df.empty or len(df) < args.min_obs:
            print(f"  {sym}: {0 if df is None else len(df)} bars < min-obs {args.min_obs}, SKIP",
                  flush=True)
            continue
        leg_dfs[sym] = df
        print(f"  fetched {sym}: {len(df)} bars", flush=True)

    rows: list[dict] = []
    for _, r in tradeable.iterrows():
        sym_y, sym_x = r.sym_y, r.sym_x
        if sym_y not in leg_dfs or sym_x not in leg_dfs:
            print(f"  {sym_y}~{sym_x}: missing leg fetch, SKIP", flush=True)
            continue
        pf = pair_frame(leg_dfs, sym_y, sym_x)
        if len(pf) < args.min_obs:
            print(f"  {sym_y}~{sym_x}: joined frame {len(pf)} < min-obs, SKIP", flush=True)
            continue
        wf = _walk_forward_pair(pf, sym_y, sym_x)
        if wf is None:
            print(f"  {sym_y}~{sym_x}: no WF result", flush=True)
            continue
        # Tier: robust requires WFE >= 0.5 AND positive OOS AND >= 10 trades — same bar as equity.
        tier = ("robust" if (wf.wfe >= 0.5 and wf.oos_score > 0 and wf.oos_trades >= 10)
                else "watch")
        rows.append({
            "sym_y": wf.sym_y, "sym_x": wf.sym_x, "n_folds": wf.n_folds,
            "oos_score": round(wf.oos_score, 3), "is_score": round(wf.is_score, 3),
            "wfe": round(wf.wfe, 3), "oos_trades": wf.oos_trades,
            "oos_win_rate": round(wf.oos_win_rate, 3),
            "oos_return": round(wf.oos_return, 4),
            "oos_max_dd": round(wf.oos_max_dd, 4),
            "best_params": str(wf.best_params), "tier": tier,
        })
        print(f"  {sym_y}~{sym_x}: OOS {wf.oos_score:.2f} WFE {wf.wfe:.2f} "
              f"trades {wf.oos_trades} -> {tier}", flush=True)

    if not rows:
        print("[fx WF] no pairs produced a WF result.", flush=True)
        return

    frame = pd.DataFrame(rows).sort_values("oos_score", ascending=False)
    args.reports_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.reports_dir / f"fx_pairs_wf_{args.tag}.csv"
    md_path = args.reports_dir / f"fx_pairs_wf_{args.tag}.md"
    frame.to_csv(csv_path, index=False)

    robust = frame[frame.tier == "robust"]
    lines = [f"# FX pairs walk-forward — {args.tag}", ""]
    lines.append(f"Walked forward {len(frame)} pairs from {input_path.name} under the FX protocol "
                  f"(train={wf_protocol('fx').train_bars}, test={wf_protocol('fx').test_bars}, "
                  f"step={wf_protocol('fx').step_bars}). "
                  f"{len(robust)} cleared the robust bar (WFE >= 0.5, OOS > 0, >= 10 trades).")
    lines.append("")
    lines.append("| y | x | folds | OOS | IS | WFE | trades | WR | ret | maxDD | tier | params |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|")
    for _, r in frame.iterrows():
        lines.append(f"| {r.sym_y} | {r.sym_x} | {int(r.n_folds)} | {r.oos_score:.2f} | "
                      f"{r.is_score:.2f} | {r.wfe:.2f} | {int(r.oos_trades)} | "
                      f"{r.oos_win_rate:.2f} | {r.oos_return:.2%} | {r.oos_max_dd:.2%} | "
                      f"{r.tier} | {r.best_params} |")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[fx WF] wrote {csv_path} and {md_path}  "
          f"({len(robust)} robust of {len(frame)} walked)", flush=True)


if __name__ == "__main__":
    main()
