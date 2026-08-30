"""Mitigate a strategy's risk with the AI model, then combine the live OSINT overlay at the edge.

Pipeline:
  1. Backtest a real strategy x symbol (cached daily data), net of cost -> strategy return stream.
  2. Walk-forward the StrategyRiskModel on that stream -> out-of-sample risk scalar (purged).
  3. Apply the scalar causally (exposure_t = scalar_{t-1}) -> AI-mitigated equity; compare Sharpe /
     Sortino / max-drawdown against the raw strategy on the same out-of-sample span.
  4. At the live edge, fetch the current OSINT class scalar and COMBINE it with the model's latest
     scalar (multiplicative, de-risk only). OSINT is live-only, so it enters here, never in the
     historical backtest.

Run:  python -m uv run python scripts/strategy_risk_demo.py --symbol AAPL --strategy macd
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from trading_live_claude.backtest.costs import CostModel
from trading_live_claude.backtest.engine import BacktestEngine
from trading_live_claude.intel.overlay import RiskOverlay
from trading_live_claude.intel.routing import classify_symbol
from trading_live_claude.models.risk_mitigation import combine
from trading_live_claude.models.strategy_risk import StrategyRiskModel
from trading_live_claude.strategies import STRATEGIES

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from osint_basket import _live_snapshot


def cached_df(symbol: str) -> pd.DataFrame:
    key = symbol.replace(".", "_").replace("/", "_")
    files = glob.glob(f"data/cache/{key}_1d_*.parquet")
    if not files:
        raise SystemExit(f"no cached data for {symbol}")
    return pd.read_parquet(max(files, key=os.path.getsize))


def perf(returns: pd.Series) -> dict[str, float]:
    r = returns.dropna()
    ann = float(r.mean() * 252)
    vol = float(r.std(ddof=0) * np.sqrt(252))
    dn = float(r[r < 0].std(ddof=0) * np.sqrt(252))
    eq = (1 + r).cumprod()
    mdd = float((eq / eq.cummax() - 1).min())
    return {"cagr": ann, "sharpe": ann / vol if vol else 0.0,
            "sortino": ann / dn if dn else 0.0, "maxdd": mdd}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="AAPL")
    ap.add_argument("--strategy", default="macd")
    ap.add_argument("--no-osint", action="store_true")
    args = ap.parse_args()

    df = cached_df(args.symbol)
    strat = STRATEGIES[args.strategy]()
    res = BacktestEngine(cost_model=CostModel.from_price(float(df["close"].iloc[-1]), is_etf=False)).run(
        strat, df, args.symbol)
    ret = res.returns
    ret.index = pd.to_datetime(df["time"].to_numpy(), utc=True)

    rk = StrategyRiskModel().walk_forward(ret)
    scalar = rk.frame["scalar"]
    oos = scalar.notna()
    raw = ret[oos]
    mitig = (ret * scalar.shift(1))[oos]     # causal: exposure uses the prior bar's scalar

    p_raw, p_mit = perf(raw), perf(mitig)
    print(f"\nAI strategy-risk mitigation — {args.strategy} on {args.symbol}  "
          f"(OOS {oos.sum()} bars, {rk.n_events} risk events)")
    print(f"  model OOS AUC {rk.oos_auc:.3f}  vs  trailing-vol baseline {rk.baseline_auc:.3f}  "
          f"({'model adds skill' if rk.oos_auc > rk.baseline_auc else 'baseline ties/wins — honest null'})\n")
    print(f"  {'metric':<10} {'raw':>10} {'AI-mitigated':>14}")
    for k, lab in [("sharpe", "Sharpe"), ("sortino", "Sortino"), ("maxdd", "Max drawdown"), ("cagr", "CAGR")]:
        fmt = (lambda v: f"{v:.2%}") if k in ("maxdd", "cagr") else (lambda v: f"{v:.2f}")
        print(f"  {lab:<10} {fmt(p_raw[k]):>10} {fmt(p_mit[k]):>14}")

    # live edge: combine the model's latest scalar with the current OSINT class scalar
    _, ai_live = StrategyRiskModel().fit_latest(ret)
    osint_dec = None
    if not args.no_osint:
        try:
            snap = asyncio.run(_live_snapshot())
            cls = classify_symbol(args.symbol)
            osint_dec = RiskOverlay().evaluate(snap)[cls]
        except Exception as e:
            print(f"\n  (OSINT unavailable: {e})")
    mit = combine(ai_live, osint_dec)
    print(f"\n  LIVE combined mitigation scalar = {mit.scalar:.0%}"
          f"  (AI x{mit.strategy_scalar:.2f}  ·  OSINT x{mit.osint_scalar:.2f})"
          f"{'  HALT' if mit.halt else ''}")
    for r_ in mit.reasons:
        print(f"    - {r_}")

    _chart(args, res, ret, scalar, oos, p_raw, p_mit, rk, mit)


def _chart(args, res, ret, scalar, oos, p_raw, p_mit, rk, mit, out="reports/strategy_risk_mitigation.png"):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    raw = ret[oos]
    mitig = (ret * scalar.shift(1))[oos]
    eq_raw = (1 + raw).cumprod()
    eq_mit = (1 + mitig).cumprod()
    idx = eq_raw.index

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 8), height_ratios=[2.4, 1], sharex=True)
    ax1.plot(idx, eq_raw, color="#8895A7", lw=1.4, label=f"raw  (Sharpe {p_raw['sharpe']:.2f}, maxDD {p_raw['maxdd']:.0%})")
    ax1.plot(idx, eq_mit, color="#1E7E74", lw=1.7, label=f"AI-mitigated  (Sharpe {p_mit['sharpe']:.2f}, maxDD {p_mit['maxdd']:.0%})")
    ax1.set_ylabel("equity (OOS, $1 start)")
    ax1.legend(loc="upper left", frameon=False, fontsize=9)
    ax1.set_title(f"AI strategy-risk mitigation — {args.strategy} on {args.symbol}\n"
                  f"model OOS AUC {rk.oos_auc:.2f} vs vol baseline {rk.baseline_auc:.2f}   ·   "
                  f"live combined scalar {mit.scalar:.0%} (AI x{mit.strategy_scalar:.2f} · OSINT x{mit.osint_scalar:.2f})",
                  fontsize=11, fontweight="bold", loc="left")
    for sp in ("top", "right"):
        ax1.spines[sp].set_visible(False)

    ax2.fill_between(idx, 0, scalar[oos], color="#C0562C", alpha=0.25, step="mid")
    ax2.plot(idx, scalar[oos], color="#C0562C", lw=1.0, drawstyle="steps-mid")
    ax2.axhline(1.0, color="#8895A7", ls=":", lw=0.8)
    ax2.set_ylim(0, 1.05)
    ax2.set_ylabel("AI risk scalar")
    ax2.set_xlabel("out-of-sample period")
    for sp in ("top", "right"):
        ax2.spines[sp].set_visible(False)
    fig.tight_layout()
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(p, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"chart -> {p}")


if __name__ == "__main__":
    main()
