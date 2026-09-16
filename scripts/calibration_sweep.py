"""Signal-statistics calibration sweep on equity + crypto — walk-forward, sortino_over_dd.

Runs the queue item at NEXT_SESSION.md §"Asset-class calibration — signal-statistics
follow-up" for the equity + crypto slice (FX is blocked on the currencies-sleeve fetch
that hasn't landed). Bollinger `n_std`, ZScoreOU `entry_z`, and RSI `oversold` are
each swept against a small basket of class-representative symbols with the same
walk-forward harness the universe sweep uses (2y train / 6mo test per WF_PROTOCOLS),
scored on `sortino_over_dd`, and the median across the basket picks the class-specific
winner for each parameter.

Purpose is *evidence* for the calibration table's next revision — this script does NOT
edit `analysis/calibration.py`. It writes `reports/calibration_sweep.csv` (per
(class, symbol, strategy, param) row) and `reports/calibration_sweep.md` (per-class
winners table). A human review then folds the winners back into the calibration
constants.

Universe:
  * Equity: EQB.TO, QQQ, XIC.TO, ENB.TO, VDY.TO — held basket, deep cache present.
  * Crypto: XBTUSD, ETHUSD, PAXGUSD — the three pairs with `_daily.parquet` built by
    `scripts/fetch_crypto_history.py`. The rest of the sleeve has tick-only caches from
    `deepen_kraken_trades.py`; building daily bars for them is a follow-up.
"""
from __future__ import annotations

import argparse
import glob
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from sweep_universe import is_etf_like, score_run    # noqa: E402

from trading_live_claude.analysis.universe import wf_protocol
from trading_live_claude.backtest import BacktestEngine
from trading_live_claude.backtest.costs import CostModel


# ---- universe + grids -------------------------------------------------------

EQUITY_UNIVERSE = ["EQB.TO", "QQQ", "XIC.TO", "ENB.TO", "VDY.TO"]
CRYPTO_UNIVERSE = ["XBTUSD", "ETHUSD", "PAXGUSD"]

STRATEGY_GRIDS: dict[str, tuple[str, list[float | int]]] = {
    # (fixed_kwargs_str, sweep_param, sweep_values). Fixed kwargs come from the current
    # calibration base for each strategy so we're moving one axis at a time.
    "bollinger":       ("window=20", [1.5, 2.0, 2.5, 3.0, 3.5]),          # n_std
    "zscore_ou":       ("window=20", [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]),    # entry_z
    "rsi_meanrevert":  ("window=14", [20, 25, 30, 35]),                    # oversold
}

STRATEGY_SWEEP_PARAM: dict[str, str] = {
    "bollinger": "n_std",
    "zscore_ou": "entry_z",
    "rsi_meanrevert": "oversold",
}


# ---- data loading -----------------------------------------------------------


def _load_equity(sym: str, cache_dir: Path) -> pd.DataFrame | None:
    """Concat shards under data/cache/{sym}_1d_*.parquet, dedup on time."""
    key = sym.replace(".", "_")
    files = sorted(glob.glob(str(cache_dir / f"{key}_1d_*.parquet")))
    if not files:
        return None
    df = pd.concat([pd.read_parquet(f) for f in files])
    df = df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    return df if not df.empty else None


def _load_crypto(pair: str, cache_dir: Path) -> pd.DataFrame | None:
    """Read data/cache/{PAIR}_daily.parquet — the aggregated form fetch_crypto_history writes."""
    p = cache_dir / f"{pair}_daily.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p).drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    return df if not df.empty else None


# ---- sweep core -------------------------------------------------------------


def _run_pinned_wf(df: pd.DataFrame, sym: str, strategy: str, params: dict,
                    asset_class: str) -> dict | None:
    """Walk-forward with PARAMS PINNED (no per-fold re-optimization).

    Standard `sweep_universe.walk_forward` picks the best (strategy, params) per fold from
    PARAM_GRIDS. For the calibration honing we need the opposite — hold params constant so
    the OOS surface is attributable to *this* param value, not to whatever the re-opt chose.
    """
    protocol = wf_protocol(asset_class)
    train_bars, test_bars, step_bars = protocol.train_bars, protocol.test_bars, protocol.step_bars

    eng = BacktestEngine(cost_model=CostModel.from_price(
        float(df["close"].iloc[-1]), is_etf=is_etf_like(sym)))

    oos_scores: list[float] = []
    is_scores: list[float] = []
    rets: list[float] = []
    dds: list[float] = []
    trades = 0
    fold_wins = 0.0
    n = len(df)
    start = 0
    while start + train_bars + test_bars <= n:
        # No re-opt — score train + test on the SAME pinned params so WFE is meaningful
        # (IS→OOS degradation for this specific config, not for whatever won the re-opt).
        tr = df.iloc[start:start + train_bars].reset_index(drop=True)
        te = df.iloc[start + train_bars:start + train_bars + test_bars].reset_index(drop=True)
        got_is = score_run(eng, strategy, params, tr, sym)
        got_te = score_run(eng, strategy, params, te, sym)
        if got_te is not None:
            oos_scores.append(got_te[0])
            is_scores.append(got_is[0] if got_is is not None else 0.0)
            rets.append(got_te[1].total_return)
            dds.append(got_te[1].max_drawdown)
            trades += got_te[1].num_trades
            fold_wins += float(got_te[1].win_rate) * got_te[1].num_trades
        start += step_bars

    if not oos_scores:
        return None
    oos_mean = float(np.mean(oos_scores))
    ins_mean = float(np.mean(is_scores))
    return {
        "oos_score": oos_mean,
        "wfe": (oos_mean / ins_mean) if ins_mean > 0 else 0.0,
        "oos_return": float(np.mean(rets)),
        "oos_maxdd": float(np.min(dds)),
        "oos_trades": int(trades),
        "oos_win_rate": (fold_wins / trades) if trades > 0 else 0.0,
        "folds": len(oos_scores),
    }


def _parse_fixed(kw_str: str) -> dict:
    """Parse 'window=20' → {'window': 20}."""
    out = {}
    for part in kw_str.split(","):
        part = part.strip()
        if not part:
            continue
        k, v = part.split("=", 1)
        v = v.strip()
        try:
            out[k.strip()] = int(v)
        except ValueError:
            try:
                out[k.strip()] = float(v)
            except ValueError:
                out[k.strip()] = v
    return out


# ---- main -------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, default=Path("data/cache"))
    ap.add_argument("--out-csv", type=Path, default=Path("reports/calibration_sweep.csv"))
    ap.add_argument("--out-md", type=Path, default=Path("reports/calibration_sweep.md"))
    ap.add_argument("--only-class", choices=("equity", "crypto", "all"), default="all")
    ap.add_argument("--only-strategy", default="",
                     help="Comma-separated: bollinger,zscore_ou,rsi_meanrevert. Empty = all.")
    args = ap.parse_args()

    classes = {"equity": EQUITY_UNIVERSE, "crypto": CRYPTO_UNIVERSE}
    if args.only_class != "all":
        classes = {args.only_class: classes[args.only_class]}
    strategies = set(STRATEGY_GRIDS)
    if args.only_strategy:
        keep = {s.strip() for s in args.only_strategy.split(",") if s.strip()}
        strategies &= keep
        if not strategies:
            raise SystemExit(f"--only-strategy filtered every strategy out. Known: {sorted(STRATEGY_GRIDS)}")

    print(f"[cal-sweep] classes={list(classes)}  strategies={sorted(strategies)}", flush=True)
    for cls, syms in classes.items():
        print(f"[cal-sweep] {cls} universe: {syms}", flush=True)

    rows: list[dict] = []
    for cls, syms in classes.items():
        loader = _load_equity if cls == "equity" else _load_crypto
        for sym in syms:
            df = loader(sym, args.cache)
            if df is None or len(df) < 400:
                print(f"[cal-sweep] {cls:>6} {sym:<10}  SKIP (no or thin cache)", flush=True)
                continue
            print(f"[cal-sweep] {cls:>6} {sym:<10}  bars={len(df)}", flush=True)
            for strategy in sorted(strategies):
                fixed_kw_str, sweep_values = STRATEGY_GRIDS[strategy]
                sweep_param = STRATEGY_SWEEP_PARAM[strategy]
                fixed = _parse_fixed(fixed_kw_str)
                for v in sweep_values:
                    params = dict(fixed)
                    params[sweep_param] = v
                    res = _run_pinned_wf(df, sym, strategy, params, cls)
                    row = {
                        "asset_class": cls, "sym": sym, "strategy": strategy,
                        "sweep_param": sweep_param, "sweep_value": v,
                        "fixed_kwargs": fixed_kw_str,
                        **(res or {"oos_score": None, "wfe": None, "oos_return": None,
                                     "oos_maxdd": None, "oos_trades": None,
                                     "oos_win_rate": None, "folds": 0}),
                    }
                    rows.append(row)
                    tag = "n/a" if res is None else f"{res['oos_score']:.3f}"
                    print(f"    {strategy:<15} {sweep_param}={v:<4}  oos_score={tag}  "
                          f"folds={0 if res is None else res['folds']}  "
                          f"trades={0 if res is None else res['oos_trades']}", flush=True)

    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    df_out = pd.DataFrame(rows)
    df_out.to_csv(args.out_csv, index=False)
    print(f"[cal-sweep] wrote {args.out_csv} ({len(df_out)} rows)", flush=True)

    # Per-class winners: median oos_score across basket per (class, strategy, sweep_value)
    winners_lines = ["# Calibration sweep — per-class winners",
                       "", "Median `oos_score` across basket per (class, strategy, sweep_value).",
                       "Winner is the sweep_value with the highest median.", ""]
    winner_map: dict[tuple[str, str], tuple[float | int, float]] = {}
    for (cls, strategy, val), grp in df_out.groupby(["asset_class", "strategy", "sweep_value"]):
        scored = grp["oos_score"].dropna()
        if scored.empty:
            continue
        med = float(scored.median())
        key = (cls, strategy)
        cur = winner_map.get(key)
        if cur is None or med > cur[1]:
            winner_map[key] = (val, med)
    # Also print the raw median surface per (class, strategy)
    for cls in sorted({r["asset_class"] for r in rows}):
        winners_lines.append(f"## {cls}")
        winners_lines.append("")
        winners_lines.append("| strategy | sweep_param | best_value | median oos_score | median WFE | full surface |")
        winners_lines.append("|---|---|---|---|---|---|")
        for strategy in sorted({r["strategy"] for r in rows if r["asset_class"] == cls}):
            sweep_param = STRATEGY_SWEEP_PARAM[strategy]
            surface: dict[float | int, tuple[float, float]] = {}
            for (c, s, v), grp in df_out.groupby(["asset_class", "strategy", "sweep_value"]):
                if c == cls and s == strategy:
                    scored = grp["oos_score"].dropna()
                    wfes = grp["wfe"].dropna()
                    if not scored.empty:
                        surface[v] = (float(scored.median()),
                                       float(wfes.median()) if not wfes.empty else 0.0)
            if not surface:
                winners_lines.append(f"| {strategy} | {sweep_param} | n/a | n/a | n/a | n/a |")
                continue
            best_v = max(surface, key=lambda k: surface[k][0])
            best_score, best_wfe = surface[best_v]
            surface_str = "  ".join(f"{v}={surface[v][0]:.3f}" for v in sorted(surface))
            winners_lines.append(f"| `{strategy}` | `{sweep_param}` | **{best_v}** | "
                                    f"{best_score:.3f} | {best_wfe:.3f} | {surface_str} |")
        winners_lines.append("")

    args.out_md.write_text("\n".join(winners_lines), encoding="utf-8")
    print(f"[cal-sweep] wrote {args.out_md}", flush=True)


if __name__ == "__main__":
    main()
