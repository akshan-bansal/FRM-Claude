"""Per-contract bar fetch for the continuous-contract pipeline.

Iterates the calendar produced by :mod:`futures_calendar`, resolves each historical contract to
an IB conid, and pulls the bars for that contract's active window. Persists one parquet per
contract under ``data/cache/futures/{ROOT}_{YYMM}.parquet`` so the fetch can be resumed
incrementally — a contract already on disk is skipped by default. That matters because IB
throttles per-endpoint at ~50-100 requests/min; a full 6y × 4 quarterly roots is 96 fetches
plus a couple of retries, right at the ceiling.

Two IB Web endpoints get used:

* ``/iserver/secdef/search`` with ``secType=FUT`` + ``symbol=<root>`` — returns the root's
  active contracts (front + a few forwards). Not enough alone for expired months.
* ``/iserver/secdef/info?conid=<root_conid>&sectype=FUT&month=YYYYMM`` — returns the specific
  contract conid for that delivery month. This is the workhorse for historical resolution.

If IB refuses expired-contract history for a given month (returns 404 / 500 / empty), the fetch
records that in the on-disk resolution map and moves on. The calibration probe
(``scripts/probe_futures_history.py``) is where that fact gets validated per exchange before a
full run kicks off.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from trading_live_claude.brokers.ib_web import IBWebBroker
from trading_live_claude.data.futures_calendar import ContractSpec, enumerate_contracts
from trading_live_claude.logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_CACHE_ROOT = Path("data/cache/futures")

# Per-request pacing. IB's history endpoint tolerates ~1 req/sec sustained; we've seen 429s at
# ~50 back-to-back with 0.5s spacing. 0.75s spacing across the pipeline stays under.
_REQUEST_PACING_S = 0.75

# Resolution cache — a JSON-shaped parquet mapping (root, year, month) → conid so a second
# run doesn't re-hit /iserver/secdef/info for contracts already resolved. Recorded even on
# failure (conid=-1) so we don't retry known-bad months.
_RESOLUTION_CACHE_PATH = DEFAULT_CACHE_ROOT / "_resolution_cache.parquet"


def _load_resolution_cache() -> dict[tuple[str, int, int], int]:
    if not _RESOLUTION_CACHE_PATH.exists():
        return {}
    df = pd.read_parquet(_RESOLUTION_CACHE_PATH)
    return {(r.root, int(r.year), int(r.month)): int(r.conid) for r in df.itertuples()}


def _save_resolution_cache(cache: dict[tuple[str, int, int], int]) -> None:
    DEFAULT_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    rows = [{"root": r, "year": y, "month": m, "conid": c}
            for (r, y, m), c in cache.items()]
    pd.DataFrame(rows).to_parquet(_RESOLUTION_CACHE_PATH, index=False)


def _resolve_historical_conid(broker: IBWebBroker, spec: ContractSpec,
                                 cache: dict[tuple[str, int, int], int]) -> int:
    """Historical (or current) conid for one (root, year, month). Cached across runs."""
    key = (spec.root, spec.year, spec.month)
    if key in cache:
        return cache[key]

    # /iserver/secdef/info with the root's conid + month works even for expired contracts on IB
    # Web (verified in Phase 1 probe). If Phase 1 fails, this call is what needs to change to a
    # different endpoint (e.g. /trsrv/secdef by ticker + month).
    yyyymm = f"{spec.year}{spec.month:02d}"
    try:
        # First: get the root's underlying conid via search
        body = broker._post("/iserver/secdef/search",
                             {"symbol": spec.root, "name": False, "secType": "FUT"})
        if not isinstance(body, list) or not body:
            cache[key] = -1
            return -1
        root_conid = int(body[0].get("conid") or 0)
        if root_conid <= 0:
            cache[key] = -1
            return -1
        # Second: pull the specific month's contract conid via secdef/info
        info = broker._get("/iserver/secdef/info",
                            {"conid": str(root_conid), "sectype": "FUT",
                             "month": yyyymm})
    except Exception as e:
        log.warning("futures.resolve_failed", root=spec.root, yyyymm=yyyymm, error=str(e))
        cache[key] = -1
        return -1

    # /iserver/secdef/info returns a LIST of contracts matching the month (usually one).
    if not isinstance(info, list) or not info:
        cache[key] = -1
        return -1
    contract_conid = int(info[0].get("conid") or 0)
    cache[key] = contract_conid if contract_conid > 0 else -1
    return cache[key]


def _fetch_contract_bars(broker: IBWebBroker, conid: int,
                          start: datetime, end: datetime) -> pd.DataFrame | None:
    """Daily bars for one contract over its active window. None on any failure so the caller
    can move to the next contract without a hard stop."""
    try:
        body = broker._get("/iserver/marketdata/history",
                            {"conid": str(conid), "period": "6m", "bar": "1d"})
    except Exception as e:
        log.warning("futures.history_failed", conid=conid, error=str(e))
        return None
    if not isinstance(body, dict):
        return None
    rows = body.get("data") or []
    if not rows:
        return None
    df = pd.DataFrame([{
        "time": datetime.fromtimestamp(int(r.get("t") or 0) / 1000.0, tz=UTC),
        "open": float(r.get("o") or 0.0),
        "high": float(r.get("h") or 0.0),
        "low": float(r.get("l") or 0.0),
        "close": float(r.get("c") or 0.0),
        "volume": int(float(r.get("v") or 0.0)),
    } for r in rows])
    # Filter to the requested active window (period=6m returns more than needed; we clip so the
    # continuous splice can trust its bounds).
    df = df[(df["time"].dt.date >= start.date()) & (df["time"].dt.date <= end.date())]
    return df.sort_values("time").reset_index(drop=True) if not df.empty else None


def contract_cache_path(spec: ContractSpec, cache_root: Path = DEFAULT_CACHE_ROOT) -> Path:
    """Filename for one per-contract parquet."""
    return cache_root / f"{spec.root}_{spec.year % 100:02d}{spec.month:02d}.parquet"


def fetch_root_history(broker: IBWebBroker, root: str, *, lookback_years: float = 6.0,
                          cache_root: Path = DEFAULT_CACHE_ROOT,
                          skip_existing: bool = True) -> list[tuple[ContractSpec, Path | None]]:
    """Pull per-contract bars for every historical + current contract of ``root``. Persists each
    to its own parquet; returns the list of (spec, path-or-None-on-failure) pairs.

    Runs politely: 0.75s pacing between requests to stay under IB's per-endpoint throttle.
    """
    cache_root.mkdir(parents=True, exist_ok=True)
    contracts = enumerate_contracts(root, lookback_years=lookback_years)
    resolution_cache = _load_resolution_cache()
    out: list[tuple[ContractSpec, Path | None]] = []
    try:
        for spec in contracts:
            path = contract_cache_path(spec, cache_root)
            if skip_existing and path.exists():
                out.append((spec, path))
                continue
            conid = _resolve_historical_conid(broker, spec, resolution_cache)
            time.sleep(_REQUEST_PACING_S)
            if conid <= 0:
                out.append((spec, None))
                continue
            df = _fetch_contract_bars(broker, conid,
                                        datetime.combine(spec.active_start, datetime.min.time(), UTC),
                                        datetime.combine(spec.active_end, datetime.min.time(), UTC))
            time.sleep(_REQUEST_PACING_S)
            if df is None or df.empty:
                out.append((spec, None))
                continue
            df.to_parquet(path, index=False)
            out.append((spec, path))
    finally:
        # Persist the resolution cache even on interruption so a rerun benefits.
        _save_resolution_cache(resolution_cache)
    return out


__all__ = [
    "DEFAULT_CACHE_ROOT",
    "contract_cache_path",
    "fetch_root_history",
]
