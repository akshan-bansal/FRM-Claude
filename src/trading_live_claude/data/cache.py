"""Parquet-backed cache for OHLCV candles. Cuts duplicate REST hits across
backtests and reduces token-refresh pressure during long live sessions.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..logging_setup import get_logger

log = get_logger(__name__)


class CandleCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _key(self, symbol: str, interval: str, start: datetime, end: datetime) -> str:
        raw = f"{symbol}|{interval}|{start.isoformat()}|{end.isoformat()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _path(self, symbol: str, interval: str, start: datetime, end: datetime) -> Path:
        safe_sym = symbol.replace(".", "_").replace("/", "_")
        return self.root / f"{safe_sym}_{interval}_{self._key(symbol, interval, start, end)}.parquet"

    def get(self, symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame | None:
        p = self._path(symbol, interval, start, end)
        if not p.exists():
            return None
        try:
            return pd.read_parquet(p)
        except Exception as e:  # pragma: no cover
            log.warning("cache.read.failed", path=str(p), error=str(e))
            return None

    def put(self, symbol: str, interval: str, start: datetime, end: datetime, df: pd.DataFrame) -> None:
        """Write atomically: to a temp sibling, then ``os.replace``. A crash mid-write used to leave a
        truncated parquet that ``get`` would then fail to read (audit gap, fixed 2026-09-18)."""
        p = self._path(symbol, interval, start, end)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        try:
            df.to_parquet(tmp, index=False)
            os.replace(tmp, p)
        except Exception as e:  # pragma: no cover
            log.warning("cache.write.failed", path=str(p), error=str(e))
            with contextlib.suppress(OSError):
                tmp.unlink()
