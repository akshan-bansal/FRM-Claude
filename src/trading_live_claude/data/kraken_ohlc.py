"""Historical OHLC candles from Kraken's public REST API.

Two entry points now, one per depth of history:

* :func:`kraken_ohlc` — one shot against ``/0/public/OHLC``. Cheap, easy, but capped at ~720
  candles per request regardless of interval (so daily reaches roughly two years). Enough to
  screen and in-sample-backtest; too short for the walk-forward gate.

* :func:`kraken_ohlc_deep` — paginate ``/0/public/Trades`` with the ``last`` nanosecond cursor
  and aggregate into daily bars. Slow (Kraken hands back ~1000 trades per page and rate-limits
  aggressively), but produces multi-year history so the crypto sleeve can clear the same 2y-train /
  6mo-test walk-forward gate the equity pool clears.

Both are public (no auth) and use ``httpx`` per the repo's HTTP convention.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pandas as pd

_KRAKEN_REST = "https://api.kraken.com/0/public/OHLC"
_KRAKEN_TRADES = "https://api.kraken.com/0/public/Trades"

# Kraken's Trades endpoint accepts a ``since`` cursor in NANOSECONDS since epoch. Documented
# elsewhere. Reasonable per-call sleep to stay under the ~1 req/s public tier: caller can tune.
_DEFAULT_SLEEP_S = 1.05


def kraken_ohlc(pair: str, *, interval: int = 1440, timeout: float = 30.0,
                client: httpx.Client | None = None) -> pd.DataFrame:
    """Daily (``interval=1440`` minutes) OHLC for a Kraken ``pair`` (e.g. ``XBTUSD``, ``ETHUSD``).

    Returns a canonical DataFrame with UTC ``time`` plus float open/high/low/close/volume, oldest
    first. Raises ``ValueError`` on a Kraken API error or an empty result.
    """
    owns = client is None
    client = client or httpx.Client(timeout=timeout, headers={"User-Agent": "FRM-Claude/1.0"})
    try:
        r = client.get(_KRAKEN_REST, params={"pair": pair, "interval": interval})
        payload = r.json()
    finally:
        if owns:
            client.close()

    if payload.get("error"):
        raise ValueError(f"Kraken OHLC error for {pair!r}: {payload['error']}")
    result = payload.get("result", {})
    series = next((v for k, v in result.items() if k != "last"), None)
    if not series:
        raise ValueError(f"Kraken OHLC returned no candles for {pair!r}")

    df = pd.DataFrame(series, columns=["time", "open", "high", "low", "close", "vwap", "volume", "count"])
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    return df[["time", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


# ---- deep history via /0/public/Trades --------------------------------------


def _fetch_trades_page(pair: str, since_ns: str, client: httpx.Client) -> tuple[list[list[object]], str]:
    """One page of the Trades endpoint. Returns (trades, next_cursor).

    Kraken's trade rows are ``[price, volume, time_seconds, side, ordertype, misc]``. ``time_seconds``
    can be a float; the cursor ``last`` is a stringified nanosecond timestamp for the NEXT call.
    Raises ``ValueError`` on an ``error`` array.
    """
    r = client.get(_KRAKEN_TRADES, params={"pair": pair, "since": since_ns})
    payload = r.json()
    if payload.get("error"):
        raise ValueError(f"Kraken Trades error for {pair!r}: {payload['error']}")
    result = payload.get("result", {})
    trades = next((v for k, v in result.items() if k != "last"), None) or []
    next_cursor = str(result.get("last", since_ns))
    return trades, next_cursor


def kraken_trades_paginated(
    pair: str,
    *,
    since_ns: str = "0",
    max_pages: int = 5000,
    client: httpx.Client | None = None,
    sleep_s: float = _DEFAULT_SLEEP_S,
    progress: Callable[[int, int], None] | None = None,
    timeout: float = 30.0,
) -> pd.DataFrame:
    """Paginate ``/0/public/Trades`` from ``since_ns`` forward and return every trade.

    Returns a canonical DataFrame ``time, price, volume, side``, oldest first. Terminates when the
    server returns an empty page OR the cursor stops advancing (both mean "no more trades in this
    window") OR ``max_pages`` is reached. ``sleep_s`` is respected between pages so a naive caller
    stays inside Kraken's public-tier rate limit.

    ``progress(page, trades_so_far)`` if supplied gets called after each page. Injectable so a
    script can print a tail and a test can assert on it without touching stdout.
    """
    owns = client is None
    client = client or httpx.Client(timeout=timeout, headers={"User-Agent": "FRM-Claude/1.0"})
    cursor = since_ns
    rows: list[list[object]] = []
    try:
        for page in range(1, max_pages + 1):
            trades, next_cursor = _fetch_trades_page(pair, cursor, client)
            if not trades:
                break
            rows.extend(trades)
            if progress is not None:
                progress(page, len(rows))
            if next_cursor == cursor:      # server ran out at this cursor
                break
            cursor = next_cursor
            if sleep_s > 0 and page < max_pages:
                time.sleep(sleep_s)
    finally:
        if owns:
            client.close()

    if not rows:
        return pd.DataFrame(columns=["time", "price", "volume", "side"])
    df = pd.DataFrame(rows, columns=["price", "volume", "time_s", "side", "ordertype", "misc"])
    df["time"] = pd.to_datetime(df["time_s"].astype(float), unit="s", utc=True)
    df["price"] = df["price"].astype(float)
    df["volume"] = df["volume"].astype(float)
    return df[["time", "price", "volume", "side"]].sort_values("time").reset_index(drop=True)


def aggregate_trades_to_daily(trades: pd.DataFrame) -> pd.DataFrame:
    """Aggregate a trades frame into daily OHLC bars.

    Canonical output columns: ``time, open, high, low, close, volume``. ``time`` is midnight UTC of
    each session; the frame is oldest-first and has no gaps for days without trades (those days
    simply don't appear rather than being forward-filled to zero volume, since that would corrupt
    every strategy that reads volume as liquidity).
    """
    if trades.empty:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    tf = trades.copy()
    tf["date"] = tf["time"].dt.floor("D")
    agg = tf.groupby("date").agg(
        open=("price", "first"),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", "last"),
        volume=("volume", "sum"),
    ).reset_index().rename(columns={"date": "time"})
    return agg[["time", "open", "high", "low", "close", "volume"]]


def kraken_ohlc_deep(
    pair: str,
    *,
    since_ns: str = "0",
    max_pages: int = 5000,
    cache_dir: Path | str | None = None,
    client: httpx.Client | None = None,
    sleep_s: float = _DEFAULT_SLEEP_S,
    progress: Callable[[int, int], None] | None = None,
) -> pd.DataFrame:
    """Deep daily history via paginated trades. Cached to parquet when ``cache_dir`` is given.

    Cache key is ``{pair}_trades.parquet`` under ``cache_dir``. When a cached file exists the
    caller can pass ``since_ns`` equal to the last trade's nanosecond timestamp to extend it
    incrementally; this function does NOT merge caches on its own (keeping the caching
    responsibility explicit at the call site rather than hidden inside a helper).
    """
    trades = kraken_trades_paginated(
        pair, since_ns=since_ns, max_pages=max_pages, client=client,
        sleep_s=sleep_s, progress=progress,
    )
    daily = aggregate_trades_to_daily(trades)
    if cache_dir is not None and not trades.empty:
        p = Path(cache_dir)
        p.mkdir(parents=True, exist_ok=True)
        trades.to_parquet(p / f"{pair}_trades.parquet", index=False)
        daily.to_parquet(p / f"{pair}_daily.parquet", index=False)
    return daily
