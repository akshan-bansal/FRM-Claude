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

from trading_live_claude.analysis.universe import HELD_ASSETS, min_oos_trades, wf_protocol
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


def walk_forward(
    df: pd.DataFrame,
    sym: str,
    train: int | None = None,
    test: int | None = None,
    step: int | None = None,
    asset_class: str | None = None,
):
    """Re-optimize on each training fold, score only the following out-of-sample block.

    Window sizing is now per-asset-class via ``analysis.universe.WF_PROTOCOLS``:
    ``train_bars`` / ``test_bars`` / ``step_bars`` come from the class's protocol unless
    explicitly overridden by the ``train`` / ``test`` / ``step`` arguments. ``asset_class`` is
    inferred from ``sym`` via ``intel.routing.classify_symbol`` if not supplied. Falls back to
    the equity protocol for unknown classes so a caller never silently misconfigures.
    """
    cls = asset_class or classify_symbol(sym.replace("_TO", ".TO").replace("_UN_", ".UN."))
    protocol = wf_protocol(cls)
    train_bars = train if train is not None else protocol.train_bars
    test_bars = test if test is not None else protocol.test_bars
    step_bars = step if step is not None else protocol.step_bars

    eng = BacktestEngine(cost_model=CostModel.from_price(float(df["close"].iloc[-1]),
                                                         is_etf=is_etf_like(sym)))
    n = len(df)
    oos_scores: list[float] = []
    is_scores: list[float] = []
    rets: list[float] = []
    dds: list[float] = []
    # Trade-weighted win rate accumulation: track wins (win_rate * num_trades per fold) and
    # total trades across all folds so the final win rate is weighted by trade count, not
    # a naive mean-of-fold-rates. A fold that fired 20 trades matters more than a fold with 2.
    fold_wins = 0.0
    trades = 0
    start = 0
    while start + train_bars + test_bars <= n:
        tr = df.iloc[start:start + train_bars].reset_index(drop=True)
        te = df.iloc[start + train_bars:start + train_bars + test_bars].reset_index(drop=True)
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
                fold_wins += float(got[1].win_rate) * got[1].num_trades
        start += step_bars
    if not oos_scores:
        return None
    oos = float(np.mean(oos_scores))
    ins = float(np.mean(is_scores))
    win_rate = (fold_wins / trades) if trades > 0 else 0.0
    return {"oos_score": oos, "wfe": oos / ins if ins > 0 else 0.0,
            "oos_return": float(np.mean(rets)), "oos_maxdd": float(np.min(dds)),
            "oos_trades": int(trades), "oos_win_rate": win_rate,
            "folds": len(oos_scores),
            "asset_class": cls, "train_bars": train_bars, "test_bars": test_bars}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=0.0)
    ap.add_argument("--max", type=float, default=1_000_000.0,
                    help="Upper price bound. Default is effectively no cap; pass a value to bracket.")
    ap.add_argument("--min-adv", type=float, default=1_000_000.0)
    ap.add_argument("--min-bars", type=int, default=900)
    ap.add_argument("--wf-top", type=int, default=30,
                    help="How many top in-sample scorers to walk-forward. Was 15; widened to 30 for "
                         "the no-price-cap resweep so promising names outside the earlier $1-45 "
                         "band actually reach stage 3.")
    ap.add_argument("--tag", default="resweep",
                    help="Filename tag for the reports (reports/sweep_{tag}_{panel,walkforward}.csv). "
                         "Bump when re-running with different filter parameters so history is preserved.")
    ap.add_argument("--carry-held/--no-carry-held", dest="carry_held", default=True,
                    action=argparse.BooleanOptionalAction,
                    help="Force currently-held names (HELD_ASSETS) into the panel and walk-forward "
                         "stages regardless of price / liquidity filters. Default ON.")
    args = ap.parse_args()

    print(f"[1/3] loading cache (>= {args.min_bars} bars)...", flush=True)
    frames = deepest_frames(args.min_bars)
    cand = screen(frames, args.min, args.max, args.min_adv)
    print(f"      {len(frames)} cached -> {len(cand)} candidates in "
          f"${args.min:.2f}-${args.max:,.0f} (ADV >= ${args.min_adv:,.0f})", flush=True)

    # Carry currently-held names in even when they fail the screen — we always want a rating on
    # what we actually own, and filter-driven silent drops are exactly the artifact a resweep is
    # meant to surface. Only add if the cache has enough bars to say anything.
    if args.carry_held:
        # Cache filenames convert dots to underscores (CGL.TO -> CGL_TO), so the frames dict is
        # keyed on the underscore form. HELD_ASSETS uses the routed dotted form (CGL.TO). Merge
        # the two conventions with a normalized lookup so a held name never silently drops out
        # of the pool over a punctuation mismatch.
        def _norm(s: str) -> str:
            return s.replace(".", "_").replace("/", "_")
        cand_keys = {_norm(str(s)) for s in cand["sym"].tolist()}
        carried: list[dict[str, object]] = []
        for sym in HELD_ASSETS:
            key = _norm(sym)
            if key in cand_keys:
                continue
            df = frames.get(key) or frames.get(sym)
            if df is None or len(df) < args.min_bars:
                print(f"      carry-held: skipping {sym} (no cached history at min-bars)",
                      flush=True)
                continue
            t = df.tail(60)
            px = float(t["close"].iloc[-1])
            adv = float((t["close"] * t["volume"]).mean()) if "volume" in t.columns else 0.0
            # Store under the underscore key so downstream frames[s] lookups work.
            carried.append({"sym": key, "price": px, "adv": adv, "vol": 0.0, "bars": len(df)})
            print(f"      carry-held: {sym} @ ${px:.2f} added to candidates", flush=True)
        if carried:
            cand = pd.concat([cand, pd.DataFrame(carried)], ignore_index=True)

    print(f"[2/3] in-sample panel over {len(cand)} names...", flush=True)
    rows = []
    for i, r in cand.iterrows():
        s = str(r["sym"])
        b = best_config(frames[s], s)
        if b is None:
            continue
        rows.append({"sym": s, "price": r["price"], "adv": r["adv"], "score": b[0],
                     "strategy": b[1], "params": b[2], "sharpe": b[3].sharpe,
                     "maxdd": b[3].max_drawdown, "trades": b[3].num_trades,
                     "held": s in HELD_ASSETS or s.replace("_", ".") in HELD_ASSETS})
        if (int(i) + 1) % 25 == 0:
            print(f"      {int(i) + 1}/{len(cand)}", flush=True)
    panel = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    panel_path = f"reports/sweep_{args.tag}_panel.csv"
    panel.to_csv(panel_path, index=False)
    print(f"      panel written ({len(panel)} scored) -> {panel_path}", flush=True)

    # Stage-3 candidates: top N by in-sample score, PLUS every held name (whether it made top-N
    # or not). Held names appearing outside the top-N have their scores read as a warning that
    # the current holding isn't in the sweep's best cohort — not a reason to drop coverage.
    lead = panel.head(args.wf_top)
    if args.carry_held:
        held_extras = panel[panel.held & ~panel.sym.isin(lead.sym.tolist())]
        if not held_extras.empty:
            lead = pd.concat([lead, held_extras], ignore_index=True)
            print(f"      carry-held: {len(held_extras)} held name(s) added below the top-{args.wf_top}",
                  flush=True)
    print(f"[3/3] walk-forward on {len(lead)} candidates...", flush=True)
    wf_rows = []
    for _, r in lead.iterrows():
        s = str(r["sym"])
        w = walk_forward(frames[s], s)
        if w is None:
            continue
        # walk_forward now returns asset_class + window sizes (from the per-class protocol) so
        # they're already in ``w``; use the protocol's own min_oos_trades bar for tiering.
        cls = w.get("asset_class") or classify_symbol(
            s.replace("_TO", ".TO").replace("_UN_", ".UN."))
        min_trades = min_oos_trades(cls)
        tier = "robust" if (w["wfe"] >= 0.5 and w["oos_score"] > 0
                            and w["oos_trades"] >= min_trades) else "watch"
        wf_rows.append({"sym": s, "price": r["price"], "is_score": r["score"],
                        "strategy": r["strategy"], "params": r["params"], **w,
                        "min_trades": min_trades, "tier": tier,
                        "held": s in HELD_ASSETS or s.replace("_", ".") in HELD_ASSETS})
        print(f"      {s}{'*' if s in HELD_ASSETS else ' '}: "
              f"OOS {w['oos_score']:.2f} WFE {w['wfe']:.2f} "
              f"trades {w['oos_trades']} -> {tier}", flush=True)
    wf = pd.DataFrame(wf_rows).sort_values("oos_score", ascending=False).reset_index(drop=True)
    wf_path = f"reports/sweep_{args.tag}_walkforward.csv"
    wf.to_csv(wf_path, index=False)
    print(f"\nrobust: {(wf.tier == 'robust').sum()}   watch: {(wf.tier == 'watch').sum()}"
          f"   held-in-pool: {int(wf['held'].sum()) if 'held' in wf.columns else 0}")
    print(wf[["sym", "held", "price", "strategy", "oos_score", "wfe", "oos_trades", "tier"]]
          .to_string(index=False, float_format=lambda v: f"{v:.2f}"))
    print(f"\nreport -> {wf_path}")


if __name__ == "__main__":
    main()
