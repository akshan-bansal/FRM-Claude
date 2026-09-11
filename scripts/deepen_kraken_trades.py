"""Deepen per-pair tick-level trade intel for the CRYPTO_SLEEVE.

Paginates Kraken's public ``/0/public/Trades`` endpoint per sleeve pair, appends the
raw tick rows (time, price, volume, side) to per-pair parquets under
``data/cache/kraken_trades/``, and prints a compact microstructure summary
(buy/sell imbalance, VWAP, trade count, notional) per pair.

Different from ``scripts/fetch_crypto_history.py``, which uses the same endpoint but
aggregates every page into a daily OHLC bar and discards the tick rows — that flow
is what the backtester eats. Microstructure intel needs the ticks themselves.

Resumable — if a per-pair parquet already exists, its last row's ns timestamp
becomes the ``since`` cursor for the next pull. From-scratch runs use
``--lookback-hours`` (default 24h) to bound the initial pull. Rate-limited to 1
request/second per Kraken's public tier.

Emits a per-run summary CSV at ``reports/kraken_trade_intel.csv`` that the intel
wing can consume later. **Does NOT write to the intel graph journal directly** —
graph edges are a design decision worth its own PR (see NEXT_SESSION.md queue).
"""
from __future__ import annotations

import argparse
import sys
import time as _time
from datetime import UTC, datetime, timedelta
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
    except Exception:                                                 # pragma: no cover
        pass

import pandas as pd

from trading_live_claude.analysis.universe import CRYPTO_SLEEVE
from trading_live_claude.data.kraken_ohlc import kraken_trades_paginated

DEFAULT_CACHE = Path("data/cache/kraken_trades")
DEFAULT_SUMMARY = Path("reports/kraken_trade_intel.csv")


def _ns_cursor_from_lookback(hours: float) -> str:
    """Kraken `since` is nanoseconds since epoch as a string.

    Bounded lookback keeps the initial pull deterministic in size — an empty cache
    plus `since=0` would try to paginate all of history at ~1s per page.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    return str(int(cutoff.timestamp() * 1_000_000_000))


def _existing_cursor(cache_path: Path) -> tuple[str | None, pd.DataFrame]:
    """Return (ns_cursor, existing_df) from a cached parquet. ns_cursor is None
    when the cache is missing or empty."""
    if not cache_path.exists():
        return None, pd.DataFrame(columns=["time", "price", "volume", "side"])
    df = pd.read_parquet(cache_path)
    if df.empty:
        return None, df
    last_ts = df["time"].iloc[-1]
    # pd.Timestamp → epoch ns. Timezone-aware ("UTC") after our loader; if naive treat as UTC.
    if last_ts.tzinfo is None:
        last_ts = last_ts.tz_localize(UTC)
    ns = int(last_ts.value)         # pandas exposes .value as ns since epoch
    return str(ns), df


def _dedup_append(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Concat + dedup on (time, price, volume, side). Kraken pages can overlap by one
    tick when the cursor lands exactly on the boundary — dedup keeps the file honest."""
    if new.empty:
        return old
    if old.empty:
        return new
    merged = pd.concat([old, new], ignore_index=True)
    merged = merged.drop_duplicates(subset=["time", "price", "volume", "side"], keep="first")
    return merged.sort_values("time").reset_index(drop=True)


def _summarize(pair: str, wire: str, df: pd.DataFrame, added: int) -> dict[str, object]:
    """Compact microstructure read for the summary CSV / stdout. Windowed to last 24h so a
    fresh cache and a deep cache produce comparable rows."""
    if df.empty:
        return {"pair": pair, "wire": wire, "rows_total": 0, "rows_added": 0,
                "window_h": 0.0, "trades_24h": 0, "buy_vol_share": None,
                "vwap_24h": None, "notional_24h_usd": None,
                "avg_trade_size": None, "latest_ts": ""}
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=24)
    recent = df[df["time"] >= pd.Timestamp(cutoff)]
    latest_ts = df["time"].iloc[-1].isoformat()
    if recent.empty:
        return {"pair": pair, "wire": wire, "rows_total": len(df), "rows_added": added,
                "window_h": 24.0, "trades_24h": 0, "buy_vol_share": None,
                "vwap_24h": None, "notional_24h_usd": None,
                "avg_trade_size": None, "latest_ts": latest_ts}
    buy_mask = recent["side"].astype(str).str.lower().str.startswith("b")
    buy_vol = float(recent.loc[buy_mask, "volume"].sum())
    total_vol = float(recent["volume"].sum())
    notional = float((recent["price"] * recent["volume"]).sum())
    vwap = notional / total_vol if total_vol > 0 else None
    return {
        "pair": pair, "wire": wire, "rows_total": len(df), "rows_added": added,
        "window_h": 24.0, "trades_24h": len(recent),
        "buy_vol_share": (buy_vol / total_vol) if total_vol > 0 else None,
        "vwap_24h": vwap, "notional_24h_usd": notional,
        "avg_trade_size": float(recent["volume"].mean()),
        "latest_ts": latest_ts,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="",
                     help="Comma-separated routed symbols (BTC/USD,ETH/USD,…) to restrict "
                          "the pull. Empty = every sleeve pair.")
    ap.add_argument("--lookback-hours", type=float, default=24.0,
                     help="Bounded initial pull when the per-pair cache is empty. Existing "
                          "caches resume from their last row's ns cursor regardless.")
    ap.add_argument("--max-pages", type=int, default=25,
                     help="Cap on paginated calls per pair per run (~1000 trades each, at "
                          "1s/page). 25 covers ~a day of BTC activity comfortably.")
    ap.add_argument("--sleep", type=float, default=1.05,
                     help="Seconds between paginated calls. Kraken public tier ~= 1s.")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--summary-csv", type=Path, default=DEFAULT_SUMMARY)
    args = ap.parse_args()

    args.cache.mkdir(parents=True, exist_ok=True)
    args.summary_csv.parent.mkdir(parents=True, exist_ok=True)

    routed_only = {s.strip().upper() for s in args.only.split(",") if s.strip()}
    entries = [
        e for e in CRYPTO_SLEEVE.values()
        if not routed_only or e.symbol.upper() in routed_only
    ]
    if not entries:
        raise SystemExit(f"--only filtered every sleeve pair out. Known: "
                         f"{sorted(e.symbol for e in CRYPTO_SLEEVE.values())}")

    from_scratch_cursor = _ns_cursor_from_lookback(args.lookback_hours)
    print(f"[deepen] pairs={[e.symbol for e in entries]}", flush=True)
    print(f"[deepen] cache_dir={args.cache}  lookback_hours={args.lookback_hours}  "
          f"max_pages={args.max_pages}  sleep={args.sleep}s", flush=True)

    summary_rows: list[dict[str, object]] = []
    started = _time.monotonic()

    for entry in entries:
        pair = entry.symbol
        wire = entry.pair
        cache_path = args.cache / f"{wire}.parquet"
        cursor, existing = _existing_cursor(cache_path)
        if cursor is None:
            cursor = from_scratch_cursor
            print(f"[deepen] {pair:>10}  ({wire:<8})  no cache — pulling from "
                  f"{args.lookback_hours}h ago (cursor={cursor})", flush=True)
        else:
            print(f"[deepen] {pair:>10}  ({wire:<8})  resuming from cache "
                  f"({len(existing)} rows, last={existing['time'].iloc[-1].isoformat()})",
                  flush=True)

        def _progress(page: int, ntrades: int, _p=pair) -> None:
            if page == 1 or page % 5 == 0:
                print(f"    {_p}: page {page:>3}, trades so far {ntrades:>6}", flush=True)

        try:
            new_df = kraken_trades_paginated(
                wire, since_ns=cursor, max_pages=args.max_pages,
                sleep_s=args.sleep, progress=_progress,
            )
        except Exception as e:
            print(f"[deepen] {pair}: FAILED — {e}", flush=True)
            summary_rows.append(_summarize(pair, wire, existing, 0))
            continue

        added = len(new_df)
        merged = _dedup_append(existing, new_df)
        if added > 0:
            merged.to_parquet(cache_path, index=False)
        summary = _summarize(pair, wire, merged, added)
        summary_rows.append(summary)
        print(f"[deepen] {pair:>10}  added={added:>5}  total={summary['rows_total']:>7}  "
              f"trades_24h={summary['trades_24h']:>5}  buy_vol_share="
              f"{'n/a' if summary['buy_vol_share'] is None else f'{summary['buy_vol_share']:.3f}'}  "
              f"vwap_24h={'n/a' if summary['vwap_24h'] is None else f'{summary['vwap_24h']:.4f}'}",
              flush=True)

    # Write summary
    if summary_rows:
        summary_df = pd.DataFrame(summary_rows)
        summary_df.to_csv(args.summary_csv, index=False)
    elapsed = _time.monotonic() - started
    print(f"[deepen] wrote {args.summary_csv}  elapsed={elapsed:.1f}s", flush=True)


if __name__ == "__main__":
    main()
