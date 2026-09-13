"""Volatility-scaled per-name notional cap: every name gets about the same annualised risk budget.

``cap = base_pct x ref_vol / vol``, clipped to ``[floor_pct, ceiling_pct]``. A name at ``ref_vol``
gets ``base_pct`` of equity; twice as volatile, half the notional. ``vol`` is the higher of short- and
long-window realised volatility, so a volatility spike tightens the cap at once. Unknown volatility
gets ``floor_pct``: the cap only loosens on evidence.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Protocol

import pandas as pd

from ..logging_setup import get_logger
from ..venues import venue_for

log = get_logger(__name__)


class _Market(Protocol):
    def recent(self, symbol: str, bars: int = 200, interval: str = "1d") -> pd.DataFrame: ...


def annualised_vol(closes: pd.Series, *, periods_per_year: float, windows: tuple[int, ...] = (20, 60),
                   ) -> float | None:
    """Max over ``windows`` of the annualised stdev of close-to-close returns; None if too short."""
    rets = closes.astype(float).pct_change().dropna()
    vols = [float(rets.tail(w).std(ddof=1)) * math.sqrt(periods_per_year)
            for w in windows if len(rets) >= w]
    vols = [v for v in vols if math.isfinite(v) and v > 0]
    return max(vols) if vols else None


class RealizedVolatility:
    """Per-symbol realised vol from daily bars, cached for ``refresh_s``."""

    def __init__(self, market: _Market, *, windows: tuple[int, ...] = (20, 60),
                 refresh_s: float = 6 * 3600, clock: Callable[[], float] = time.monotonic) -> None:
        self.market = market
        self.windows = windows
        self.refresh_s = refresh_s
        self._clock = clock
        self._cache: dict[str, tuple[float | None, float]] = {}

    def __call__(self, symbol: str) -> float | None:
        now = self._clock()
        cached = self._cache.get(symbol)
        if cached is not None and now - cached[1] < self.refresh_s:
            return cached[0]
        periods = 365.0 if venue_for(symbol)[0].always_open else 252.0
        try:
            df = self.market.recent(symbol, bars=max(self.windows) + 5, interval="1d")
            vol = annualised_vol(df["close"], periods_per_year=periods, windows=self.windows)
        except Exception as e:
            log.warning("position_cap.vol_failed", symbol=symbol, error=str(e))
            vol = None
        self._cache[symbol] = (vol, now)
        return vol


class VolScaledPositionCap:
    def __init__(self, vol_for: Callable[[str], float | None], *, base_pct: float = 0.50,
                 ref_vol: float = 0.20, floor_pct: float = 0.05, ceiling_pct: float = 0.75) -> None:
        self.vol_for = vol_for
        self.base_pct = base_pct
        self.ref_vol = ref_vol
        self.floor_pct = floor_pct
        self.ceiling_pct = ceiling_pct

    def __call__(self, symbol: str) -> float:
        vol = self.vol_for(symbol)
        if vol is None or not math.isfinite(vol) or vol <= 0:
            return self.floor_pct
        return min(max(self.base_pct * self.ref_vol / vol, self.floor_pct), self.ceiling_pct)


def position_cap_for(settings: object, market: _Market) -> Callable[[str], float] | None:
    """The per-name cap the paper entry points use, per ``position_cap_mode`` in trading.yaml."""
    if getattr(settings, "position_cap_mode", "static") != "vol_scaled":
        return None
    return VolScaledPositionCap(
        RealizedVolatility(market),
        base_pct=settings.max_position_notional_pct,          # type: ignore[attr-defined]
        ref_vol=settings.position_cap_ref_vol,                # type: ignore[attr-defined]
        floor_pct=settings.position_cap_floor_pct,            # type: ignore[attr-defined]
        ceiling_pct=settings.position_cap_ceiling_pct,        # type: ignore[attr-defined]
    )
