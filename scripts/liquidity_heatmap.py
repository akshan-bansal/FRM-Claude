"""Trading volume / liquidity heat map — hour-of-day × day-of-week per asset class.

Reveals the intraday liquidity surface: London-open surge in FX, NY-close futures peak, crypto
weekend thin-books, Asian-hours dead zones. One heat map per asset class, plus a combined
markdown report with per-asset commentary on the hottest / coldest buckets.

Data sources per class:

* **equity, precious_metals, commodity, fixed_income**: IB Web ``/iserver/marketdata/history``
  with ``bar=1h period=90d`` per symbol. Needs CP Gateway auth'd. Rate-limited at 0.75s/request
  by ``IBWebBroker`` — a 20-symbol run takes ~2 min.
* **futures**: same IB Web endpoint but resolves each root as FUT front-month via
  ``set_sec_type(root, "FUT")``. Front-month data is real live volume; roll gaps are ignored for
  the heat map since we're aggregating on hour+weekday, not on absolute date.
* **crypto**: Kraken ``/public/OHLC`` with ``interval=60`` (hourly bars). One call per pair,
  immediate, no auth.
* **fx**: same Kraken OHLC endpoint on the FX pair codes.

Normalization: per-asset volume is divided by that asset's OWN mean-across-all-buckets so the
color scale represents "% of typical volume in this hour/weekday for this name". A cell at 1.5
means "50% heavier than the asset's average bucket"; 0.5 means "half as much". Cross-asset
comparison in the same heat map would be misleading — the biggest cap always dominates — so we
render one row per asset with per-row normalization.

Output:

* ``reports/liquidity_heatmap_<class>_<tag>.png`` — one PNG per class
* ``reports/liquidity_heatmap_<tag>.md`` — combined report with a table of the top-3 heaviest
  and coldest buckets per asset, plus one-line commentary on the observable pattern
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
    except Exception:
        pass


AssetClass = Literal["equity", "futures", "fixed_income", "precious_metals",
                       "commodity", "crypto", "fx"]

# Default universe per class — mirrors the actively-traded baskets in this repo (paper monitors +
# validated pool). Override at runtime with --symbols.
DEFAULT_UNIVERSE: dict[AssetClass, tuple[str, ...]] = {
    "equity":          ("QQQ", "SPY", "IWM", "XIC.TO", "VDY.TO", "EQB.TO"),
    "futures":         ("ES", "NQ", "CL", "GC", "ZN"),
    "fixed_income":    ("TLT", "IEF", "SHY", "LQD"),
    "precious_metals": ("GLD", "SLV", "CGL.TO"),
    "commodity":       ("USO", "UNG", "DBC"),
    "crypto":          ("XBTUSD", "ETHUSD", "PAXGUSD", "XMRUSD", "XRPUSD", "XLMUSD", "LINKUSD",
                        "SOLUSD", "ADAUSD", "POLUSD", "UNIUSD", "AAVEUSD", "MKRUSD", "ZECUSD"),
    "fx":              ("EURGBP", "EURCAD", "EURCHF", "EURJPY"),
}


def _fetch_ib_hourly(symbols: tuple[str, ...], as_futures: bool = False) -> dict[str, pd.DataFrame]:
    """Pull ~90 days of hourly bars per symbol from IB Web. Returns {symbol: DataFrame}."""
    from trading_live_claude.brokers.ib_web import CPGatewayAuth, IBWebBroker
    from trading_live_claude.config import get_settings

    settings = get_settings()
    auth = CPGatewayAuth(host=settings.ib_web_host, port=settings.ib_web_port,
                          verify_ssl=settings.ib_web_verify_ssl)
    broker = IBWebBroker(auth=auth, enable_live_orders=False)
    # Fail fast if not auth'd
    try:
        broker.accounts()
    except Exception as e:
        raise SystemExit(f"[liq-heatmap] IB Gateway not authenticated: {e}") from e

    if as_futures:
        for sym in symbols:
            broker.set_sec_type(sym, "FUT")

    out: dict[str, pd.DataFrame] = {}
    end = datetime.now(UTC)
    start = end - timedelta(days=90)
    for sym in symbols:
        print(f"[liq-heatmap]   fetching {sym} hourly (90d)...", flush=True)
        try:
            bars = broker.candles(sym, start, end, interval="OneHour")
        except Exception as e:
            print(f"[liq-heatmap]   {sym}: fetch failed ({type(e).__name__}: {e})", flush=True)
            continue
        if not bars:
            print(f"[liq-heatmap]   {sym}: empty response", flush=True)
            continue
        df = pd.DataFrame([{"time": b.start, "volume": b.volume} for b in bars])
        if df.empty:
            continue
        out[sym] = df
    return out


def _fetch_qt_hourly(symbols: tuple[str, ...]) -> dict[str, pd.DataFrame]:
    """Pull ~30 days of hourly bars per symbol from Questrade. Returns {symbol: DataFrame}.

    Questrade caps intraday history at ~30 days. That's ~22 trading days × 6.5 regular-
    hours cells per weekday = ~130 (hour, weekday) observations per symbol, or ~4-5 obs
    per cell — the same sparse-but-visual regime as the Kraken crypto branch.
    """
    from trading_live_claude.brokers.questrade import QuestradeBroker
    from trading_live_claude.config import get_settings

    settings = get_settings()
    broker = QuestradeBroker.from_settings(
        refresh_token=settings.questrade_refresh_token,
        encryption_key=settings.token_encryption_key,
        state_dir=settings.state_dir,
    )
    out: dict[str, pd.DataFrame] = {}
    end = datetime.now(UTC)
    start = end - timedelta(days=30)
    for sym in symbols:
        print(f"[liq-heatmap]   fetching {sym} hourly (30d, QT)...", flush=True)
        try:
            bars = broker.candles(sym, start, end, interval="OneHour")
        except Exception as e:
            print(f"[liq-heatmap]   {sym}: fetch failed ({type(e).__name__}: {e})", flush=True)
            continue
        if not bars:
            print(f"[liq-heatmap]   {sym}: empty response", flush=True)
            continue
        df = pd.DataFrame([{"time": b.start, "volume": b.volume} for b in bars])
        if df.empty:
            continue
        out[sym] = df
    return out


def _fetch_kraken_hourly(pairs: tuple[str, ...]) -> dict[str, pd.DataFrame]:
    """Pull hourly OHLC per pair from Kraken. One call each."""
    from trading_live_claude.data.kraken_ohlc import kraken_ohlc

    out: dict[str, pd.DataFrame] = {}
    for pair in pairs:
        print(f"[liq-heatmap]   fetching {pair} hourly...", flush=True)
        try:
            df = kraken_ohlc(pair, interval=60)     # 60-minute bars
        except Exception as e:
            print(f"[liq-heatmap]   {pair}: fetch failed ({type(e).__name__}: {e})", flush=True)
            continue
        if df is None or df.empty:
            continue
        # Normalize columns to (time, volume) so the aggregator is class-agnostic
        out[pair] = df[["time", "volume"]].copy()
    return out


def _build_heatmap(df: pd.DataFrame) -> np.ndarray:
    """Aggregate a (time, volume) frame into a 24 × 7 array of mean volume per (hour, weekday).

    Rows: hour 0..23 (UTC); Cols: weekday 0=Mon .. 6=Sun.
    Returns the raw (unnormalized) mean-volume matrix.
    """
    d = df.copy()
    d["time"] = pd.to_datetime(d["time"], utc=True)
    d["hour"] = d["time"].dt.hour
    d["weekday"] = d["time"].dt.weekday          # Mon=0
    grid = d.groupby(["hour", "weekday"])["volume"].mean().unstack(fill_value=0.0)
    # Reindex to fill missing hours/weekdays with 0 so the heat map is a full 24 × 7 rectangle
    out = np.zeros((24, 7), dtype=float)
    for h in range(24):
        for w in range(7):
            if h in grid.index and w in grid.columns:
                out[h, w] = float(grid.at[h, w])
    return out


def _render_class_png(class_name: str, hm_per_asset: dict[str, np.ndarray],
                        out_path: Path) -> None:
    """One PNG per class: stacked heat maps, one row per asset, per-row normalization."""
    if not hm_per_asset:
        print(f"[liq-heatmap] {class_name}: no data to plot", flush=True)
        return
    import matplotlib.pyplot as plt

    n = len(hm_per_asset)
    fig, axes = plt.subplots(n, 1, figsize=(8, 2.4 * n), squeeze=False)
    weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for i, (asset, hm) in enumerate(hm_per_asset.items()):
        ax = axes[i, 0]
        norm = hm / (hm.mean() or 1.0)           # per-asset normalization
        im = ax.imshow(norm, aspect="auto", cmap="viridis", vmin=0, vmax=2.0, origin="lower")
        ax.set_xticks(range(7))
        ax.set_xticklabels(weekday_labels, fontsize=8)
        ax.set_yticks(range(0, 24, 2))
        ax.set_yticklabels([f"{h:02d}h" for h in range(0, 24, 2)], fontsize=7)
        ax.set_title(f"{asset}  (asset-normalized: 1.0 = typical bucket)", fontsize=9)
    fig.colorbar(im, ax=axes.ravel().tolist(), shrink=0.6, label="× typical")
    fig.suptitle(f"Liquidity heat map — {class_name}  (hour UTC × weekday, mean volume)",
                  fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.98))
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    print(f"[liq-heatmap] {class_name}: wrote {out_path}", flush=True)


def _hot_cold_report(hm_per_asset: dict[str, np.ndarray]) -> list[str]:
    """Per-asset text: top-3 hottest and coldest (hour, weekday) buckets."""
    lines: list[str] = []
    weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for asset, hm in hm_per_asset.items():
        norm = hm / (hm.mean() or 1.0)
        # Flatten, sort by value; take top-3 hot + top-3 cold (excluding zeros)
        flat: list[tuple[float, int, int]] = [
            (float(norm[h, w]), h, w) for h in range(24) for w in range(7) if norm[h, w] > 0
        ]
        flat.sort()
        cold = flat[:3]
        hot = flat[-3:][::-1]
        lines.append(f"### {asset}")
        lines.append("**Hottest buckets** (× typical):")
        for v, h, w in hot:
            lines.append(f"- {weekday_labels[w]} {h:02d}:00 UTC — {v:.2f}×")
        lines.append("")
        lines.append("**Coldest buckets** (× typical, non-zero only):")
        for v, h, w in cold:
            lines.append(f"- {weekday_labels[w]} {h:02d}:00 UTC — {v:.2f}×")
        lines.append("")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--classes", default="equity,futures,fixed_income,precious_metals,commodity,crypto,fx",
                    help="Comma-separated asset classes to include. Default: all 7.")
    ap.add_argument("--symbols", default="",
                    help="Override the default universe for ONE class. Combine with --only-class.")
    ap.add_argument("--only-class", default="",
                    help="Restrict to a single asset class (paired with --symbols).")
    ap.add_argument("--equity-source", choices=("ib", "qt"), default="ib",
                    help="Which broker feeds the equity class. Default 'ib' (90d hourly via IB Web, "
                         "needs CP Gateway auth). 'qt' uses Questrade (30d hourly, needs refresh "
                         "token in .env). Only affects the equity class; other classes are unchanged.")
    ap.add_argument("--tag", default=date.today().isoformat())
    ap.add_argument("--reports-dir", type=Path, default=Path("reports"))
    args = ap.parse_args()

    args.reports_dir.mkdir(parents=True, exist_ok=True)
    classes = [c.strip() for c in args.classes.split(",") if c.strip()]

    if args.only_class:
        classes = [args.only_class]
        if args.symbols:
            DEFAULT_UNIVERSE[args.only_class] = tuple(  # type: ignore[index]
                s.strip() for s in args.symbols.split(",") if s.strip())

    ib_classes = {"equity", "fixed_income", "precious_metals", "commodity"}
    all_class_heatmaps: dict[str, dict[str, np.ndarray]] = {}

    for cls in classes:
        print(f"\n[liq-heatmap] === class={cls} ===", flush=True)
        symbols = DEFAULT_UNIVERSE.get(cls, ())     # type: ignore[arg-type]
        if not symbols:
            print(f"[liq-heatmap]   no universe for class {cls!r}, skipping", flush=True)
            continue
        if cls == "equity" and args.equity_source == "qt":
            data = _fetch_qt_hourly(symbols)
        elif cls in ib_classes:
            data = _fetch_ib_hourly(symbols, as_futures=False)
        elif cls == "futures":
            data = _fetch_ib_hourly(symbols, as_futures=True)
        elif cls in {"crypto", "fx"}:
            data = _fetch_kraken_hourly(symbols)
        else:
            print(f"[liq-heatmap]   unknown class {cls!r}, skipping", flush=True)
            continue

        hm_per_asset = {sym: _build_heatmap(df) for sym, df in data.items() if not df.empty}
        all_class_heatmaps[cls] = hm_per_asset
        png_path = args.reports_dir / f"liquidity_heatmap_{cls}_{args.tag}.png"
        _render_class_png(cls, hm_per_asset, png_path)

    # Combined markdown
    md_path = args.reports_dir / f"liquidity_heatmap_{args.tag}.md"
    lines = [f"# Liquidity heat map — {args.tag}", ""]
    lines.append("Per-asset hour-of-day × day-of-week volume aggregation. Each bucket normalized")
    lines.append("to the asset's own mean-across-all-buckets, so `1.5×` means 'the volume in that")
    lines.append("(hour, weekday) bucket typically runs 50% heavier than this asset's average bucket'.")
    lines.append("")
    for cls, hm_per_asset in all_class_heatmaps.items():
        if not hm_per_asset:
            continue
        lines.append(f"## {cls}")
        lines.append(f"![]({(args.reports_dir / f'liquidity_heatmap_{cls}_{args.tag}.png').name})")
        lines.append("")
        lines.extend(_hot_cold_report(hm_per_asset))
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[liq-heatmap] wrote combined report {md_path}", flush=True)


if __name__ == "__main__":
    main()
