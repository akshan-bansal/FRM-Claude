"""Validate the OSINT-overlaid 50-asset basket against real returns data.

The overlay is live-only, so this is NOT a through-time backtest of it (that would be lookahead —
WorldMonitor has no point-in-time history). Instead it is an ex-ante risk cross-check that uses the
returns data we actually hold: for every basket asset with cached daily history (equities, ETFs) or
public Kraken history (crypto), compute realized annualized volatility, then ask two questions:

1. Does the overlay de-risk in the right direction? Higher-realized-vol classes should draw the
   lower risk scalars (a negative rank correlation between class vol and class scalar).
2. Does applying today's live overlay lower the book's ex-ante volatility versus equal weight?

Assets without returns data (most futures roots and FX pairs here) are reported and excluded.

Run:  python -m uv run python scripts/validate_osint_basket.py
"""
from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from trading_live_claude.intel.overlay import OverlayClass, RiskOverlay

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # allow `import osint_basket` from any cwd
from osint_basket import BASKET, CLASS_COLOR, CLASS_ORDER, get_snapshot


def _cached_closes(symbol: str) -> pd.Series | None:
    key = symbol.replace(".", "_").replace("/", "_")
    files = glob.glob(f"data/cache/{key}_1d_*.parquet")
    if not files:
        return None
    f = max(files, key=os.path.getsize)  # the deepest history we cached for it
    df = pd.read_parquet(f)
    s = df.set_index("time")["close"].astype(float)
    s.index = pd.to_datetime(s.index, utc=True)
    return s


def _kraken_closes(pair: str) -> pd.Series | None:
    try:
        from trading_live_claude.data.kraken_ohlc import kraken_ohlc
        df = kraken_ohlc(pair)
        s = df.set_index("time")["close"].astype(float)
        s.index = pd.to_datetime(s.index, utc=True)
        return s
    except Exception:
        return None


def _closes_for(cls: OverlayClass, symbol: str) -> pd.Series | None:
    if cls == "crypto":
        return _kraken_closes(symbol)
    return _cached_closes(symbol)


def main() -> None:
    snap, src = get_snapshot("--from-journal" in sys.argv)
    print(f"  [snapshot source: {src}]")
    decisions = RiskOverlay().evaluate(snap)

    closes: dict[str, pd.Series] = {}
    cls_of: dict[str, OverlayClass] = {}
    missing: dict[OverlayClass, list[str]] = {c: [] for c in CLASS_ORDER}
    for cls in CLASS_ORDER:
        for sym in BASKET[cls]:
            s = _closes_for(cls, sym)
            if s is not None and len(s) > 260:
                s = s.tail(504)               # ~2y
                s.index = s.index.normalize()  # floor to calendar day so crypto/equity align
                closes[sym] = s[~s.index.duplicated(keep="last")]
                cls_of[sym] = cls
            else:
                missing[cls].append(sym)

    # realized annualized vol per asset
    vol: dict[str, float] = {}
    for sym, s in closes.items():
        r = s.pct_change().dropna()
        vol[sym] = float(r.std(ddof=0) * np.sqrt(252.0))

    # per-class mean realized vol and scalar
    print(f"\nOSINT overlay validation vs realized returns  (strategic-risk {snap.strategic_risk:.0f}, "
          f"fear/greed {snap.fear_greed}, VIX {snap.market.get('equity_vol')})\n")
    print(f"{'class':<10} {'scalar':>7} {'realized vol':>13} {'n assets':>9}  {'no data':>8}")
    class_vol: dict[OverlayClass, float] = {}
    rows = []
    for cls in CLASS_ORDER:
        syms = [s for s in closes if cls_of[s] == cls]
        cv = float(np.mean([vol[s] for s in syms])) if syms else float("nan")
        class_vol[cls] = cv
        rows.append((cls, decisions[cls].scalar, cv, len(syms), len(missing[cls])))
        cvs = f"{cv:.1%}" if syms else "  n/a"
        print(f"{cls:<10} {decisions[cls].scalar:>6.0%} {cvs:>13} {len(syms):>9}  {len(missing[cls]):>8}")

    # (1) direction check: rank corr between class vol and class scalar (want negative)
    have = [c for c in CLASS_ORDER if not np.isnan(class_vol[c])]
    if len(have) >= 3:
        v = pd.Series({c: class_vol[c] for c in have}).rank()
        sc = pd.Series({c: decisions[c].scalar for c in have}).rank()
        rho = float(v.corr(sc, method="spearman"))
        verdict = "aligned (more vol -> more de-risk)" if rho < 0 else "NOT aligned"
        print(f"\n(1) vol vs scalar rank corr = {rho:+.2f}  -> {verdict}")

    # (2) ex-ante portfolio vol: equal-weight vs OSINT-adjusted, on the realized covariance
    px = pd.concat({s: closes[s] for s in closes}, axis=1, sort=False).sort_index()
    R = px.pct_change().dropna(how="any")
    if len(R) > 60 and R.shape[1] >= 5:
        cov = R.cov().values * 252.0
        syms = list(R.columns)
        base = 1.0 / (sum(len(v) for v in BASKET.values()))  # 1/50, so cash is explicit
        w_eq = np.array([base for _ in syms])
        w_adj = np.array([base * decisions[cls_of[s]].scalar for s in syms])
        vol_eq = float(np.sqrt(w_eq @ cov @ w_eq))
        vol_adj = float(np.sqrt(w_adj @ cov @ w_adj))
        print(f"\n(2) ex-ante annualized book vol on {len(syms)} priced assets:")
        print(f"    equal-weight   gross {w_eq.sum():.0%}  vol {vol_eq:.2%}")
        print(f"    OSINT-adjusted gross {w_adj.sum():.0%}  vol {vol_adj:.2%}"
              f"   ({(vol_adj / vol_eq - 1) * 100:+.0f}% vs equal-weight)")

    _chart(rows, snap)


def _chart(rows, snap, out="reports/osint_validation.png") -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    have = [r for r in rows if not np.isnan(r[2])]
    classes = [r[0] for r in have]
    vols = [r[2] for r in have]
    scalars = [r[1] for r in have]
    x = np.arange(len(classes))

    fig, ax1 = plt.subplots(figsize=(9.5, 5.2))
    ax1.bar(x, vols, color=[CLASS_COLOR[c] for c in classes], width=0.6, zorder=2, alpha=0.9)
    ax1.set_ylabel("realized annualized volatility (2y)  — bars", color="#182130")
    ax1.set_xticks(x)
    ax1.set_xticklabels([c.upper() for c in classes])
    ax1.set_ylim(0, max(vols) * 1.22)
    for xi, v in zip(x, vols, strict=True):
        ax1.text(xi, v + max(vols) * 0.02, f"{v:.0%} vol", ha="center", fontsize=9,
                 color="#182130", fontweight="bold")

    ax2 = ax1.twinx()
    ax2.plot(x, scalars, "o-", color="#111", lw=1.8, zorder=4, markersize=7)
    ax2.set_ylabel("OSINT overlay scalar (de-risk)  — line", color="#111")
    ax2.set_ylim(0, 1.12)
    for xi, s in zip(x, scalars, strict=True):
        ax2.annotate(f"x{s:.2f}", (xi, s), textcoords="offset points", xytext=(0, 13),
                     ha="center", fontsize=9, color="#111", fontweight="bold",
                     bbox=dict(boxstyle="round,pad=0.22", fc="white", ec="#CBD2DA", lw=0.6))

    # Where the overlay de-risks for a reason volatility cannot explain, say so on the chart.
    accel = getattr(snap, "event_acceleration", {}) or {}
    hot = [f"{d} {a:.1f}x" for d, a in accel.items() if a >= 1.5]
    note = ("exogenous event flow: " + ", ".join(hot)) if hot else "no event-flow surge"
    ax1.set_title("Validation: realized vol (bars) vs live OSINT de-risk (line)\n"
                  f"vol alignment is only part of it — {note} de-risks independently of volatility",
                  fontsize=11, fontweight="bold", loc="left")
    for sp in ("top",):
        ax1.spines[sp].set_visible(False)
        ax2.spines[sp].set_visible(False)
    fig.tight_layout()
    p = Path(out)
    fig.savefig(p, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"chart -> {p}")


if __name__ == "__main__":
    main()
