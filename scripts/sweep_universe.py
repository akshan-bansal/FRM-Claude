"""Sweep the cached asset universe in a price band and walk-forward the leaders.

Three stages, mirroring how earlier price-band sweeps were run in this project but committed this
time instead of ad-hoc:

1. **Screen** — every symbol in the local candle cache with enough history, filtered to a price band
   plus liquidity (average dollar volume) and a sane volatility range.
2. **In-sample panel** — for each survivor, grid-search every strategy in ``PARAM_GRIDS`` net of a
   realistic per-name cost model and keep its best (strategy, params) by the ``sortino_over_dd``
   objective. In-sample scores oversell, so this only ranks candidates for stage 3.
3. **Walk-forward** — 2y train / 6mo test, step 6mo, parameters re-optimized on every fold and scored
   ONLY out-of-sample. ``robust`` requires WFE >= 0.5 AND positive OOS score AND >= 10 OOS trades;
   everything else is ``watch``. This is the only stage whose numbers are trustworthy.

Run:  python -m uv run python scripts/sweep_universe.py --min 1 --max 45
"""
from __future__ import annotations

import argparse
import itertools
import os
import re
import warnings

import numpy as np
import pandas as pd

from trading_live_claude.analysis.universe import min_oos_trades
from trading_live_claude.backtest import BacktestEngine
from trading_live_claude.backtest.costs import CostModel
from trading_live_claude.intel.routing import classify_symbol
from trading_live_claude.optimize import PARAM_GRIDS
from trading_live_claude.scoring.objective import ObjectiveAdapter, ObjectiveInput
from trading_live_claude.strategies import STRATEGIES

warnings.filterwarnings("ignore")

CACHE = "data/cache"
OBJ = ObjectiveAdapter.from_name("sortino_over_dd")


def deepest_frames(min_bars: int) -> dict[str, pd.DataFrame]:
    """Load the deepest cached daily frame per symbol."""
    best: dict[str, tuple[str, int]] = {}
    with os.scandir(CACHE) as it:
        for e in it:
            m = re.match(r"(.+?)_1d_[0-9a-f]+\.parquet$", e.name)
            if not m:
                continue
            s, sz = m.group(1), e.stat().st_size
            if s not in best or sz > best[s][1]:
                best[s] = (e.path, sz)
    out: dict[str, pd.DataFrame] = {}
    for s, (p, _) in best.items():
        try:
            df = pd.read_parquet(p)
        except Exception:      # a corrupt cache entry must not kill the sweep
            continue
        if not df.empty and "close" in df.columns and len(df) >= min_bars:
            out[s] = df
    return out


def screen(frames: dict[str, pd.DataFrame], lo: float, hi: float, min_adv: float) -> pd.DataFrame:
    rows = []
    for s, df in frames.items():
        t = df.tail(60)
        px = float(t["close"].iloc[-1])
        adv = float((t["close"] * t["volume"]).mean()) if "volume" in t.columns else 0.0
        r = t["close"].pct_change().dropna()
        vol = float(r.std(ddof=0) * np.sqrt(252)) if len(r) else 0.0
        rows.append((s, px, adv, vol, len(df)))
    d = pd.DataFrame(rows, columns=["sym", "price", "adv", "vol", "bars"])
    keep = d[(d.price >= lo) & (d.price <= hi) & (d.adv >= min_adv) & d.vol.between(0.05, 1.5)]
    return keep.sort_values("adv", ascending=False).reset_index(drop=True)


def combos(grid: dict) -> list[dict]:
    keys = list(grid)
    out = []
    for c in itertools.product(*[grid[k] for k in keys]):
        p = dict(zip(keys, c, strict=True))
        if "fast" in p and "slow" in p and p["fast"] >= p["slow"]:
            continue
        if "entry_window" in p and "exit_window" in p and p["exit_window"] > p["entry_window"]:
            continue
        out.append(p)
    return out


def score_run(eng: BacktestEngine, sname: str, params: dict, df: pd.DataFrame, sym: str):
    try:
        res = eng.run(STRATEGIES[sname](**params), df, sym)
    except Exception:
        return None
    m = res.metrics
    return OBJ.score(ObjectiveInput.from_metrics(m)), m


# Explicit ETF membership. A first-letter heuristic is wrong in the direction that matters: it
# silently costs single-name equities (VALE, XOM, V) at ETF spreads, understating their friction by
# roughly a quarter and flattering exactly the marginal names a sweep is meant to filter out. When in
# doubt this returns False, so an unknown symbol is charged the *wider* equity spread.
_ETF_PREFIXES = ("XL", "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "VEA", "VWO", "EFA", "EEM",
                 "GLD", "SLV", "USO", "UNG", "DBC", "DBA", "CORN", "WEAT", "IAU", "PSLV", "CPER")
_ETF_CA_PREFIXES = ("X", "Z", "V", "H")   # iShares / BMO / Vanguard / Horizons, .TO-listed only


def is_etf_like(sym: str) -> bool:
    """Whether to price this symbol with ETF spreads. Conservative: unknown -> False (equity)."""
    s = sym.upper()
    if s.startswith(_ETF_PREFIXES):
        return True
    # Canadian ETF families are only identifiable together with the .TO listing suffix.
    return s.endswith((".TO", "_TO")) and s.startswith(_ETF_CA_PREFIXES)


def best_config(df: pd.DataFrame, sym: str):
    """In-sample best (strategy, params) for one symbol, net of cost."""
    eng = BacktestEngine(cost_model=CostModel.from_price(float(df["close"].iloc[-1]),
                                                         is_etf=is_etf_like(sym)))
    best = None
    for sname, grid in PARAM_GRIDS.items():
        for p in combos(grid):
            got = score_run(eng, sname, p, df, sym)
            if got is None:
                continue
            if best is None or got[0] > best[0]:
                best = (got[0], sname, p, got[1])
    return best


def walk_forward(df: pd.DataFrame, sym: str, train: int = 504, test: int = 126):
    """Re-optimize on each training fold, score only the following out-of-sample block."""
    eng = BacktestEngine(cost_model=CostModel.from_price(float(df["close"].iloc[-1]),
                                                         is_etf=is_etf_like(sym)))
    n = len(df)
    oos_scores: list[float] = []
    is_scores: list[float] = []
    rets: list[float] = []
    dds: list[float] = []
    trades = 0
    start = 0
    while start + train + test <= n:
        tr = df.iloc[start:start + train].reset_index(drop=True)
        te = df.iloc[start + train:start + train + test].reset_index(drop=True)
        bt = None
        for sname, grid in PARAM_GRIDS.items():
            for p in combos(grid):
                got = score_run(eng, sname, p, tr, sym)
                if got and (bt is None or got[0] > bt[0]):
                    bt = (got[0], sname, p)
        if bt is not None:
            got = score_run(eng, bt[1], bt[2], te, sym)
            if got is not None:
                oos_scores.append(got[0])
                is_scores.append(bt[0])
                rets.append(got[1].total_return)
                dds.append(got[1].max_drawdown)
                trades += got[1].num_trades
        start += test
    if not oos_scores:
        return None
    oos = float(np.mean(oos_scores))
    ins = float(np.mean(is_scores))
    return {"oos_score": oos, "wfe": oos / ins if ins > 0 else 0.0,
            "oos_return": float(np.mean(rets)), "oos_maxdd": float(np.min(dds)),
            "oos_trades": int(trades), "folds": len(oos_scores)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=1.0)
    ap.add_argument("--max", type=float, default=45.0)
    ap.add_argument("--min-adv", type=float, default=1_000_000.0)
    ap.add_argument("--min-bars", type=int, default=900)
    ap.add_argument("--wf-top", type=int, default=15)
    args = ap.parse_args()

    print(f"[1/3] loading cache (>= {args.min_bars} bars)...", flush=True)
    frames = deepest_frames(args.min_bars)
    cand = screen(frames, args.min, args.max, args.min_adv)
    print(f"      {len(frames)} cached -> {len(cand)} candidates in "
          f"${args.min:.0f}-{args.max:.0f} (ADV >= ${args.min_adv:,.0f})", flush=True)

    print(f"[2/3] in-sample panel over {len(cand)} names...", flush=True)
    rows = []
    for i, r in cand.iterrows():
        s = str(r["sym"])
        b = best_config(frames[s], s)
        if b is None:
            continue
        rows.append({"sym": s, "price": r["price"], "adv": r["adv"], "score": b[0],
                     "strategy": b[1], "params": b[2], "sharpe": b[3].sharpe,
                     "maxdd": b[3].max_drawdown, "trades": b[3].num_trades})
        if (int(i) + 1) % 25 == 0:
            print(f"      {int(i) + 1}/{len(cand)}", flush=True)
    panel = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    panel.to_csv("reports/sweep_1_45_panel.csv", index=False)
    print(f"      panel written ({len(panel)} scored)", flush=True)

    lead = panel.head(args.wf_top)
    print(f"[3/3] walk-forward on top {len(lead)}...", flush=True)
    wf_rows = []
    for _, r in lead.iterrows():
        s = str(r["sym"])
        w = walk_forward(frames[s], s)
        if w is None:
            continue
        # Trade-count bar is asset-class dependent: commodities trade on a slower cycle, so a
        # flat 10 excluded the whole class (see universe.min_oos_trades).
        cls = classify_symbol(s.replace("_TO", ".TO").replace("_UN_", ".UN."))
        tier = "robust" if (w["wfe"] >= 0.5 and w["oos_score"] > 0
                            and w["oos_trades"] >= min_oos_trades(cls)) else "watch"
        wf_rows.append({"sym": s, "price": r["price"], "is_score": r["score"],
                        "strategy": r["strategy"], "params": r["params"], **w,
                        "asset_class": cls, "min_trades": min_oos_trades(cls), "tier": tier})
        print(f"      {s}: OOS {w['oos_score']:.2f} WFE {w['wfe']:.2f} "
              f"trades {w['oos_trades']} -> {tier}", flush=True)
    wf = pd.DataFrame(wf_rows).sort_values("oos_score", ascending=False).reset_index(drop=True)
    wf.to_csv("reports/sweep_1_45_walkforward.csv", index=False)
    print(f"\nrobust: {(wf.tier == 'robust').sum()}   watch: {(wf.tier == 'watch').sum()}")
    print(wf[["sym", "price", "strategy", "oos_score", "wfe", "oos_trades", "tier"]]
          .to_string(index=False, float_format=lambda v: f"{v:.2f}"))


if __name__ == "__main__":
    main()
