"""Fetch multi-year daily bars for every pair in CRYPTO_SLEEVE via Kraken's paginated Trades API.

Preparation step for :mod:`walk_forward_crypto`. Slow — Kraken's public tier caps requests at
roughly 1/second and hands back ~1000 trades per page, so a from-zero pull of BTC/USD takes hours
even on a good day. Cached to parquet under ``data/cache`` so re-runs pick up where the last one
stopped, resumable via the ``--since`` argument.

Usage examples::

    # from scratch, all sleeve pairs, be prepared to wait
    python scripts/fetch_crypto_history.py

    # one pair, resume from a nanosecond timestamp printed by an earlier run
    python scripts/fetch_crypto_history.py --pair XBTUSD --since 1699000000000000000

    # bounded — a few pages per pair, useful for a smoke test
    python scripts/fetch_crypto_history.py --max-pages 3

Checkpointing (2026-09-23). A pull is done in chunks of ``--chunk-pages``; after each chunk the
trades are merged into ``<PAIR>_trades.parquet``, re-aggregated into ``<PAIR>_daily.parquet``, and
the cursor is written to ``<PAIR>_history.json``. A later run with no explicit ``--since`` resumes
from that cursor. This replaced the older overwrite-only behaviour after a 2026-09-23 XMRUSD run
died on a connection reset at 9.93M trades and lost ~2.4h of fetching, because the parquet was
only written at the very end.

``--max-pages`` caps ONE run (default 7500, about 2h at ~1 req/s), not the pair: a pair needing
more simply takes several runs, each resuming where the last stopped. The pair is marked
``complete`` in the checkpoint when the endpoint stops advancing the cursor, and a complete pair
is skipped unless ``--restart`` is passed. De-duplication is explicit: chunks are concatenated,
sorted by time and de-duplicated on (time, price, volume, side), so an overlapping window at a
chunk boundary cannot double-count a trade.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from trading_live_claude.analysis.universe import CRYPTO_SLEEVE
from trading_live_claude.data.kraken_ohlc import (
    aggregate_trades_to_daily,
    kraken_trades_paginated,
)

DEFAULT_CACHE = Path("data/cache")
TRADE_KEY = ["time", "price", "volume", "side"]


def checkpoint_path(cache: Path, pair: str) -> Path:
    return cache / f"{pair}_history.json"


def read_checkpoint(cache: Path, pair: str) -> dict:
    p = checkpoint_path(cache, pair)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}                      # unreadable checkpoint = start over rather than crash
    return data if isinstance(data, dict) else {}


def write_json_atomic(path: Path, payload: dict) -> None:
    """Write via a temp file + replace so a crash mid-write can't leave a torn checkpoint."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def write_parquet_atomic(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def merge_trades(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    """Concatenate, sort oldest-first and drop duplicate trades at the chunk boundary."""
    frames = [f for f in (old, new) if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame(columns=TRADE_KEY)
    out = pd.concat(frames, ignore_index=True)
    return (out.sort_values("time", kind="stable")
               .drop_duplicates(subset=TRADE_KEY, keep="first")
               .reset_index(drop=True))


def cursor_ns(trades: pd.DataFrame) -> str:
    """Kraken's ``since`` cursor: the last trade's timestamp in nanoseconds."""
    return str(pd.Timestamp(trades["time"].iloc[-1]).value)


def _progress(pair: str) -> callable[[int, int], None]:
    def cb(page: int, trades: int) -> None:
        if page % 10 == 0 or page == 1:
            print(f"    {pair}: page {page:>5}, trades so far {trades:>7}", flush=True)
    return cb


def fetch_pair(pair: str, cache: Path, *, since: str | None, max_pages: int, chunk_pages: int,
               sleep_s: float) -> dict:
    """Fetch one pair in resumable chunks. Returns the checkpoint written."""
    ck = read_checkpoint(cache, pair)
    trades_path = cache / f"{pair}_trades.parquet"
    daily_path = cache / f"{pair}_daily.parquet"
    cursor = since if since is not None else str(ck.get("cursor_ns", "0"))
    have: pd.DataFrame | None = None
    if trades_path.exists() and since is None:
        have = pd.read_parquet(trades_path)
        print(f"[fetch] {pair}: resuming from cursor {cursor} ({len(have):,} trades cached)",
              flush=True)

    pages_left, chunk_no, complete, failure = max_pages, 0, False, ""
    while pages_left > 0 and not complete:
        chunk_no += 1
        take = min(chunk_pages, pages_left)
        try:
            new_trades = kraken_trades_paginated(pair, since_ns=cursor, max_pages=take,
                                                 sleep_s=sleep_s, progress=_progress(pair))
        except Exception as e:                       # network reset, 5xx, malformed page
            # Everything up to the previous chunk is already on disk; stop cleanly and keep it.
            failure = f"{type(e).__name__}: {e}"
            print(f"[fetch] {pair}: chunk {chunk_no} FAILED — {failure}; "
                  f"keeping the {chunk_no - 1} chunk(s) already written", flush=True)
            break
        pages_left -= take
        if new_trades.empty:
            complete = True
            print(f"[fetch] {pair}: endpoint returned no further trades — complete", flush=True)
            break
        merged = merge_trades(have, new_trades)
        advanced = have is None or len(merged) > len(have)
        have = merged
        next_cursor = cursor_ns(merged)
        complete = not advanced or next_cursor == cursor
        cursor = next_cursor
        write_parquet_atomic(merged, trades_path)
        daily = aggregate_trades_to_daily(merged)
        write_parquet_atomic(daily, daily_path)
        ck = {"cursor_ns": cursor, "trades_total": len(merged),
              "bars": len(daily), "complete": bool(complete),
              "last_bar": str(daily["time"].iloc[-1].date()) if len(daily) else "",
              "updated": datetime.now(UTC).isoformat()}
        write_json_atomic(checkpoint_path(cache, pair), ck)
        print(f"[fetch] {pair}: chunk {chunk_no} saved — {len(merged):,} trades, "
              f"{len(daily)} bars through {ck['last_bar']}, cursor {cursor}, "
              f"{pages_left} pages left in this run", flush=True)
    if failure:
        ck = {**ck, "last_error": failure, "updated": datetime.now(UTC).isoformat()}
        write_json_atomic(checkpoint_path(cache, pair), ck)
    return ck


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", help="Fetch just this Kraken wire pair (e.g. XBTUSD).")
    ap.add_argument("--since", default=None,
                    help="Nanosecond cursor to start from. Omit to resume from the pair's "
                         "checkpoint (0 = earliest, when there is none).")
    ap.add_argument("--max-pages", type=int, default=7500,
                    help="Cap for THIS run, not the pair — a pair needing more takes several "
                         "runs, each resuming from the checkpoint. ~2h at 7500 pages.")
    ap.add_argument("--chunk-pages", type=int, default=250,
                    help="Pages per checkpoint. Smaller = less lost to a mid-run failure, "
                         "more parquet rewrites.")
    ap.add_argument("--restart", action="store_true",
                    help="Ignore the checkpoint and refetch the pair from the earliest trade.")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--sleep", type=float, default=1.05, help="Seconds between pages.")
    args = ap.parse_args()

    args.cache.mkdir(parents=True, exist_ok=True)
    pairs_to_fetch: list[str] = (
        [args.pair] if args.pair
        else [entry.pair for entry in CRYPTO_SLEEVE.values()]
    )
    print(f"[fetch] cache={args.cache}  pairs={pairs_to_fetch}  "
          f"max_pages={args.max_pages}/run  chunk={args.chunk_pages}  sleep={args.sleep}s",
          flush=True)

    for pair in pairs_to_fetch:
        ck = read_checkpoint(args.cache, pair)
        if ck.get("complete") and not args.restart:
            print(f"[fetch] {pair}: already complete ({ck.get('trades_total', 0):,} trades, "
                  f"through {ck.get('last_bar', '?')}); --restart to refetch", flush=True)
            continue
        since = args.since if args.since is not None else ("0" if args.restart else None)
        print(f"[fetch] {pair} ...", flush=True)
        out = fetch_pair(pair, args.cache, since=since, max_pages=args.max_pages,
                         chunk_pages=args.chunk_pages, sleep_s=args.sleep)
        if not out:
            print(f"[fetch] {pair}: nothing written.", flush=True)
            continue
        state = "COMPLETE" if out.get("complete") else "PARTIAL (resume with another run)"
        print(f"[fetch] {pair}: {state} — {out.get('trades_total', 0):,} trades, "
              f"{out.get('bars', 0)} daily bars through {out.get('last_bar', '?')}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
