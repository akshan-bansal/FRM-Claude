"""Pre-warm the local candle cache for a list of symbols via Questrade.

Precondition for the sweep: ``scripts/sweep_universe.py`` reads ``data/cache/*.parquet`` and
skips anything with too few bars. If a name isn't cached at all, it's silently dropped from the
sweep — the exact artifact the held-assets carry-in was supposed to prevent. This script fixes
that by fetching history first, so a resweep sees every name the seed and the held list expect.

Usage examples::

    # warm every held name plus the entire expanded SEED_UNIVERSE equity list
    python scripts/warm_cache.py --held --seed equity

    # a specific list
    python scripts/warm_cache.py --symbols XLE,XLF,XLK

    # commodity + crypto sleeves — crypto currently uses the equities feed for those routed
    # names that Questrade won't recognize; that's expected to skip fast
    python scripts/warm_cache.py --seed commodity

Slow. Questrade rate-limits per second and each symbol is one candle call. A 100-name warmup
takes several minutes. Skips symbols already cached at the requested depth so re-runs are cheap.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading_live_claude.analysis.universe import HELD_ASSETS, SEED_UNIVERSE
from trading_live_claude.brokers.questrade import QuestradeBroker
from trading_live_claude.config import get_settings
from trading_live_claude.data.cache import CandleCache
from trading_live_claude.data.market import MarketData


def _make_broker() -> QuestradeBroker:
    """Same construction as cli._make_questrade — no positional argument, keyword-only fields."""
    s = get_settings()
    return QuestradeBroker.from_settings(
        refresh_token=s.questrade_refresh_token,
        encryption_key=s.token_encryption_key,
        state_dir=s.state_dir,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="", help="Comma-separated symbols. Combined with --seed / --held.")
    ap.add_argument("--seed", default="", choices=("", "equity", "commodity", "future", "crypto"),
                    help="Include every SEED_UNIVERSE symbol of the given class.")
    ap.add_argument("--held", action="store_true", help="Include every HELD_ASSETS symbol.")
    ap.add_argument("--years", type=float, default=5.0,
                    help="How much daily history to request (Questrade caps large windows).")
    ap.add_argument("--interval", default="1d")
    args = ap.parse_args()

    targets: set[str] = set()
    for s in args.symbols.split(","):
        s = s.strip()
        if s:
            targets.add(s.upper())
    if args.seed:
        targets.update(SEED_UNIVERSE[args.seed])       # type: ignore[index]
    if args.held:
        targets.update(HELD_ASSETS)
    if not targets:
        raise SystemExit("Nothing to warm. Pass --symbols, --seed, and/or --held.")

    print(f"[warm] {len(targets)} symbols requested", flush=True)
    settings = get_settings()
    broker = _make_broker()
    market = MarketData(broker, cache=CandleCache(settings.data_cache_dir))
    end = datetime.now(UTC)

    ok = 0
    skipped = 0
    failed = 0
    for i, sym in enumerate(sorted(targets), 1):
        try:
            df = market.history(sym, years=args.years, interval=args.interval, end=end)
            if df.empty:
                print(f"  [{i}/{len(targets)}] {sym}: empty result — skip", flush=True)
                skipped += 1
                continue
            print(f"  [{i}/{len(targets)}] {sym}: {len(df)} bars "
                  f"({df['time'].iloc[0].date()} -> {df['time'].iloc[-1].date()})", flush=True)
            ok += 1
        except Exception as e:
            print(f"  [{i}/{len(targets)}] {sym}: FAILED — {e}", flush=True)
            failed += 1

    print(f"\n[warm] done: {ok} ok, {skipped} empty, {failed} failed", flush=True)


if __name__ == "__main__":
    main()
