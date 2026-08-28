"""Historical OHLC candles from Kraken's public REST API.

The Kraken WebSocket feeds (:mod:`...microstructure.kraken_l2`) are live-only; to run currency
pairs through the same research flow as the equity basket (screen -> backtest -> score) we need
history. Kraken's ``/0/public/OHLC`` endpoint returns candles in the project's canonical shape
(time/open/high/low/close/volume, UTC). It is public — no auth — and uses ``httpx`` per the repo's
HTTP convention.

Caveat: the endpoint returns at most ~720 candles per request regardless of ``interval``, so daily
data covers roughly the last two years. That is enough to screen and backtest a pair; a full
multi-fold walk-forward wants more history than this endpoint gives.
"""
from __future__ import annotations

import httpx
import pandas as pd

_KRAKEN_REST = "https://api.kraken.com/0/public/OHLC"


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
