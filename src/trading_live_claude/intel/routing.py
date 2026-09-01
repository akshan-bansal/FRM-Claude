"""Route monitored symbols to overlay asset classes, and provide a refreshing overlay callable.

The live monitor asks ``overlay_for(symbol)`` for the current de-risk decision that applies to a
symbol. That needs two things: a map from symbol to overlay asset class (``classify_symbol``), and a
periodically-refreshed set of :class:`~trading_live_claude.intel.overlay.OverlayDecision`s built from
a live snapshot (:class:`OverlayProvider`). The provider is deliberately fail-safe: if a refresh
raises (network hiccup, expired key) it keeps the last good decisions, and if it has never had a
good read it returns ``None`` so the monitor behaves exactly as if no overlay were configured.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping

from trading_live_claude.intel.history import append_snapshot
from trading_live_claude.intel.overlay import (
    IntelSnapshot,
    OverlayClass,
    OverlayDecision,
    RiskOverlay,
)
from trading_live_claude.logging_setup import get_logger

log = get_logger(__name__)

# Bare tickers that are crypto even without a pair separator.
_CRYPTO_BASES = {"BTC", "XBT", "ETH", "XMR", "XRP", "XLM", "LINK", "SOL", "ADA", "DOGE", "PAXG"}
# ETFs / symbols that track a physical commodity (not the miners, which trade as equities).
_COMMODITY_SYMBOLS = {"CGL.TO", "CGL-C.TO", "GLD", "IAU", "SLV", "PSLV", "USO", "UNG", "DBC", "GSG"}
# Common FX quote/base codes, for detecting 6-letter pairs like EURUSD / USDCAD.
_FX_CODES = {"USD", "CAD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "MXN", "CNH", "SEK", "NOK"}


def classify_symbol(symbol: str, overrides: Mapping[str, OverlayClass] | None = None) -> OverlayClass:
    """Best-effort overlay asset class for a symbol. Explicit ``overrides`` always win.

    Heuristics (in order): a Kraken-style ``BASE/QUOTE`` or crypto base -> ``crypto``; a 6-letter
    FX pair of known currency codes -> ``fx``; a leading ``/`` futures root -> ``future``; a known
    physical-commodity ETF -> ``commodity``; everything else -> ``equity`` (the common case here).
    """
    s = symbol.strip().upper()
    if overrides and s in {k.upper(): v for k, v in overrides.items()}:
        return {k.upper(): v for k, v in overrides.items()}[s]
    if s.startswith("/"):
        return "future"   # futures root notation (/ES) — before the crypto "/" check
    if "/" in s or s.split("-")[0] in _CRYPTO_BASES:
        return "crypto"
    if len(s) == 6 and s[:3] in _FX_CODES and s[3:] in _FX_CODES:
        return "fx"
    if s in _COMMODITY_SYMBOLS:
        return "commodity"
    return "equity"


class OverlayProvider:
    """Callable ``(symbol) -> OverlayDecision | None`` backed by a refreshing live snapshot.

    ``snapshot_fn`` is a *synchronous* zero-arg callable returning a fresh :class:`IntelSnapshot`
    (the CLI wraps the async WorldMonitor client in one). Decisions are recomputed at most every
    ``refresh_seconds``; failures are swallowed so the monitor loop never breaks on intel I/O.
    """

    def __init__(self, snapshot_fn: Callable[[], IntelSnapshot], *, refresh_seconds: float = 900.0,
                 overlay: RiskOverlay | None = None,
                 class_overrides: Mapping[str, OverlayClass] | None = None,
                 journal: bool = True) -> None:
        self._snapshot_fn = snapshot_fn
        self._refresh = refresh_seconds
        self._overlay = overlay or RiskOverlay()
        self._overrides = class_overrides
        self._journal = journal
        self._decisions: dict[OverlayClass, OverlayDecision] | None = None
        self._ts = 0.0

    def refresh(self, *, force: bool = False) -> None:
        if not force and self._decisions is not None and (time.monotonic() - self._ts) < self._refresh:
            return
        try:
            snap = self._snapshot_fn()
            self._decisions = self._overlay.evaluate(snap)
            self._ts = time.monotonic()
            # Every live read goes into the journal — the monitor is the highest-frequency consumer
            # of the feed, so it is where the point-in-time history actually accrues.
            if self._journal:
                append_snapshot(snap, self._decisions)
            log.info("overlay.refreshed", degraded=snap.degraded,
                     scalars={c: d.scalar for c, d in self._decisions.items()})
        except Exception as e:  # keep last good decisions; never break the monitor
            log.warning("overlay.refresh_failed", error=str(e))

    def __call__(self, symbol: str) -> OverlayDecision | None:
        self.refresh()
        if self._decisions is None:
            return None
        return self._decisions.get(classify_symbol(symbol, self._overrides))
