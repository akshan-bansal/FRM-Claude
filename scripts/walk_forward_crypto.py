"""Walk-forward the crypto sleeve against multi-year daily bars.

Runs the same walk-forward gate the equity pool clears — 2y train / 6mo test, per-fold re-opt,
robust threshold WFE >= 0.5 AND positive OOS AND >= 10 OOS trades — on the seven pairs in
:data:`trading_live_claude.analysis.universe.CRYPTO_SLEEVE`. Reads cached deep-history parquets
(one per pair) written by :func:`trading_live_claude.data.kraken_ohlc.kraken_ohlc_deep`; skips a
pair with no cache or insufficient bars rather than triggering the fetch here (the fetch is
network-bound and belongs in a dedicated preparation step).

Annualization note. The equity walk-forward assumes 252 trading days/year; crypto trades 365. The
objective adapter this script reuses (``sortino_over_dd``) is a *ratio* of Sortino to max drawdown
and does not depend on the annualization constant — so the numbers here are comparable to the
equity pool without a code change. If a future objective introduces annualized-return scaling,
this script will need to switch to 365 explicitly.

Output. A ranked table on stdout and ``reports/walk_forward_crypto.csv``. This does NOT flip the
tier field in ``CRYPTO_SLEEVE``; the intent is that a human reads the report and edits universe.py
after inspection. Automating the promotion would silently bind a live sleeve to whatever the last
Kraken pull said, and that is a change big enough to want on a human's diff.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from trading_live_claude.analysis.universe import CRYPTO_SLEEVE
from trading_live_claude.data.kraken_ohlc import kraken_ohlc

# Reuse the exact walk-forward helper the equity sweep uses so the numbers stay comparable.
sys.path.insert(0, str(Path(__file__).parent))
from sweep_universe import walk_forward       # noqa: E402


DEFAULT_CACHE = Path("data/cache")
MIN_BARS_DEEP = 900              # about 2.5 years — enough for two 6-mo test folds
MIN_BARS_SHALLOW = 500           # Kraken /public/OHLC caps at ~720 bars → ~3.9 folds — the honest
                                  # minimum for a WFE calculation, thinner than deep-history WF but
                                  # real out-of-sample scoring with real cost accounting.
REPORT_PATH = Path("reports/walk_forward_crypto.csv")


def _load_cached_daily(pair_code: str, cache_dir: Path,
                        min_bars: int = MIN_BARS_DEEP) -> pd.DataFrame | None:
    """Load ``{pair_code}_daily.parquet`` (as written by kraken_ohlc_deep) if it exists and has bars."""
    p = cache_dir / f"{pair_code}_daily.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if df.empty or len(df) < min_bars:
        return None
    return df


def _load_shallow_daily(pair_code: str, min_bars: int = MIN_BARS_SHALLOW) -> pd.DataFrame | None:
    """Fetch shallow (~720-bar) daily OHLC directly from Kraken's /public/OHLC as a fallback for
    pairs without a deep-history parquet. One call per pair, ~1 second, no caching.

    Kraken's cap of ~720 bars means the walk-forward has only ~3.9 folds (720 - 365 train)/91 step,
    which is thinner than the 12+ folds a deep-history run supports. Rows scored this way are
    reported with tier=``screened+`` — better than pure in-sample, not ``robust`` until deep
    history clears them. Runs the same walk-forward helper, so the OOS scores stay comparable.
    """
    try:
        df = kraken_ohlc(pair_code, interval=1440)          # 1440 min = daily
    except Exception as e:
        print(f"  shallow fetch failed for {pair_code}: {type(e).__name__}: {e}", flush=True)
        return None
    if df is None or df.empty or len(df) < min_bars:
        return None
    # kraken_ohlc returns columns time / open / high / low / close / volume — the shape the equity
    # sweep's walk_forward expects.
    return df


def _crypto_min_oos_trades() -> int:
    """Trade-count bar for the crypto sleeve. Same 10 the equity default uses.

    Crypto trades 365 days/year, so 10 OOS trades over a 6mo test window is a lower relative bar
    than the same 10 on an equity strategy — the deeper sample is a feature, not a reason to raise
    the bar. Left at 10 to keep the promotion criterion unified across classes.
    """
    return 10


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE,
                    help="Directory of {pair}_daily.parquet caches (from kraken_ohlc_deep).")
    ap.add_argument("--min-bars", type=int, default=MIN_BARS_DEEP,
                    help="Minimum bars for deep-history tier. Deep parquets below this are skipped; "
                         "the shallow-fallback threshold (MIN_BARS_SHALLOW = 500) is applied "
                         "separately when the deep parquet is absent.")
    ap.add_argument("--no-shallow-fallback", dest="shallow_fallback", action="store_false",
                    default=True,
                    help="Disable the shallow /public/OHLC fallback and only score pairs with a "
                         "deep-history parquet. Default is to fall back — a shallow WF (tier="
                         "'screened+') is better than a full skip.")
    args = ap.parse_args()

    print(f"[crypto WF] cache={args.cache} min_bars_deep={args.min_bars} "
          f"shallow_fallback={args.shallow_fallback}", flush=True)
    rows: list[dict[str, object]] = []
    for routed, entry in CRYPTO_SLEEVE.items():
        df = _load_cached_daily(entry.pair, args.cache, min_bars=args.min_bars)
        source = "deep"
        if df is None and args.shallow_fallback:
            df = _load_shallow_daily(entry.pair)
            source = "shallow"
        if df is None:
            print(f"  {routed}: no usable history for {entry.pair} — SKIP", flush=True)
            continue
        wf = walk_forward(df, entry.pair)
        if wf is None:
            print(f"  {routed}: no walk-forward result — SKIP", flush=True)
            continue
        bar = _crypto_min_oos_trades()
        # Shallow-fallback pairs are capped at "screened+" — better than pure in-sample, thinner
        # than deep-history WF, and NOT ``robust`` until the deep-history fetch clears them.
        if source == "shallow":
            tier = "screened+" if wf["oos_score"] > 0 else "watch"
        else:
            tier = ("robust" if (wf["wfe"] >= 0.5 and wf["oos_score"] > 0
                                 and wf["oos_trades"] >= bar) else "watch")
        row = {"symbol": routed, "pair": entry.pair, "screen_score": entry.screen_score,
               "source": source, **wf, "min_trades": bar, "tier": tier}
        rows.append(row)
        print(f"  {routed:>10}: [{source:7s}] OOS {wf['oos_score']:.2f} WFE {wf['wfe']:.2f} "
              f"trades {wf['oos_trades']} -> {tier}", flush=True)

    if not rows:
        print("[crypto WF] no pairs had usable cached history — run fetch_crypto_history first.",
              flush=True)
        return
    frame = pd.DataFrame(rows).sort_values("oos_score", ascending=False)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(REPORT_PATH, index=False)
    print(f"[crypto WF] {len(rows)} pairs scored -> {REPORT_PATH}", flush=True)
    robust = frame[frame.tier == "robust"]
    if not robust.empty:
        print("[crypto WF] robust survivors (candidates to promote in CRYPTO_SLEEVE):", flush=True)
        for _, r in robust.iterrows():
            print(f"     {r['symbol']:>10}  OOS {r['oos_score']:.2f}  WFE {r['wfe']:.2f}  "
                  f"trades {int(r['oos_trades'])}", flush=True)
    else:
        print("[crypto WF] no robust survivors — screen scores were weak, so this is expected. "
              "Anything above the watch bar is worth carrying with a size cap.", flush=True)


if __name__ == "__main__":
    main()
