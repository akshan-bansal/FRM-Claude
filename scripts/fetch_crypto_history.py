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

Idempotency. Rerunning with the same ``--since`` (or without one) overwrites the parquet on disk
with a fresh full pull; there is no merge step here. That is deliberate — an incremental merge
would need de-duplication logic that quietly fails when the endpoint returns overlapping windows,
and it is safer to make the caller explicit about "extend cache from this cursor" than to hide it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from trading_live_claude.analysis.universe import CRYPTO_SLEEVE
from trading_live_claude.data.kraken_ohlc import kraken_ohlc_deep

DEFAULT_CACHE = Path("data/cache")


def _progress(pair: str) -> "callable[[int, int], None]":
    def cb(page: int, trades: int) -> None:
        if page % 10 == 0 or page == 1:
            print(f"    {pair}: page {page:>5}, trades so far {trades:>7}", flush=True)
    return cb


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", help="Fetch just this Kraken wire pair (e.g. XBTUSD).")
    ap.add_argument("--since", default="0",
                    help="Nanosecond cursor to start from. 0 = earliest available.")
    ap.add_argument("--max-pages", type=int, default=5000)
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--sleep", type=float, default=1.05, help="Seconds between pages.")
    args = ap.parse_args()

    args.cache.mkdir(parents=True, exist_ok=True)
    pairs_to_fetch: list[str] = (
        [args.pair] if args.pair
        else [entry.pair for entry in CRYPTO_SLEEVE.values()]
    )
    print(f"[fetch] cache={args.cache}  pairs={pairs_to_fetch}  since={args.since}  "
          f"max_pages={args.max_pages}  sleep={args.sleep}s", flush=True)

    for pair in pairs_to_fetch:
        print(f"[fetch] {pair} ...", flush=True)
        try:
            daily = kraken_ohlc_deep(
                pair,
                since_ns=args.since,
                max_pages=args.max_pages,
                cache_dir=args.cache,
                sleep_s=args.sleep,
                progress=_progress(pair),
            )
        except Exception as e:
            print(f"[fetch] {pair}: FAILED — {e}", flush=True)
            continue
        if daily.empty:
            print(f"[fetch] {pair}: no bars written (endpoint returned no trades).", flush=True)
            continue
        print(f"[fetch] {pair}: wrote {len(daily)} daily bars "
              f"({daily['time'].iloc[0].date()} → {daily['time'].iloc[-1].date()})", flush=True)


if __name__ == "__main__":
    sys.exit(main())
