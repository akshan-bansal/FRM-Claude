"""Route monitored symbols to overlay asset classes, and provide a refreshing overlay callable.

The live monitor asks ``overlay_for(symbol)`` for the current de-risk decision that applies to a
symbol. That needs two things: a map from symbol to overlay asset class (``classify_symbol``), and a
periodically-refreshed set of :class:`~trading_live_claude.intel.overlay.OverlayDecision`s built from
a live snapshot (:class:`OverlayProvider`). The provider is deliberately fail-safe: if a refresh
raises (network hiccup, expired key) it keeps the last good decisions, and if it has never had a
good read it returns ``None`` so the monitor behaves exactly as if no overlay were configured.

This module also provides :class:`PersistenceGate`, an entry-side halt built on the intel graph's
``edge_persistence`` query. The rule it enforces: an entry in a symbol whose overlay class is
exposed to domain X only fires if X has NOT been persistently elevated for N or more consecutive
polls. This turns the difference between "one 6x event-acceleration spike is noise" and "the same
6x reading across five polls is a regime" from a docstring into an enforceable check.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from pathlib import Path

from trading_live_claude.intel.graph import (
    DEFAULT_GRAPH_JOURNAL,
    edge_persistence,
    load_edges,
)
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
# ETFs / symbols that track a physical commodity broad basket (not the miners, which trade as
# equities). Precious metals split into their own class below — they carry a distinct safe-haven
# character (strong dxy inverse correlation, muted risk-off trimming) that the broad-commodity
# overlay does not model.
_COMMODITY_SYMBOLS = {"USO", "UNG", "DBC", "GSG", "DBA"}
# Physical precious-metals ETFs. Safe-haven behaviour distinct from broad commodities: rallies on
# conflict / disaster / weakening USD, still gets partial trimming in a global risk-off squeeze.
_PRECIOUS_METALS_SYMBOLS = {"GLD", "IAU", "SLV", "PSLV", "SGOL", "SIVR", "PPLT", "PALL",
                              "CGL.TO", "CGL-C.TO"}
# Fixed-income ETFs — treasuries, munis, corporate bonds, aggregates. Rate/credit exposure has its
# own overlay branch: bonds usually rally in risk-off (flight-to-quality), so their scalar de-risks
# more gently than equity for the same news.
_FIXED_INCOME_SYMBOLS = {
    "TLT", "IEF", "SHY", "TLH", "IEI", "GOVT",              # US treasuries by duration
    "BND", "AGG", "MBB",                                     # aggregates + mortgage
    "LQD", "HYG", "JNK", "EMB",                              # corporate + EM debt
    "XBB.TO", "ZAG.TO", "VAB.TO", "ZFL.TO", "ZDB.TO",       # Canadian bond ETFs
    "MUB", "TFI",                                            # US municipal
}
# Common FX quote/base codes, for detecting 6-letter pairs like EURUSD / USDCAD.
_FX_CODES = {"USD", "CAD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "MXN", "CNH", "SEK", "NOK"}


def classify_symbol(symbol: str, overrides: Mapping[str, OverlayClass] | None = None) -> OverlayClass:
    """Best-effort overlay asset class for a symbol. Explicit ``overrides`` always win.

    Heuristics (in order): a Kraken-style ``BASE/QUOTE`` or crypto base -> ``crypto``; a 6-letter
    FX pair of known currency codes -> ``fx``; a leading ``/`` futures root -> ``future``; a known
    fixed-income ETF -> ``fixed_income``; a known precious-metals ETF -> ``precious_metals``; a
    known broad-commodity ETF -> ``commodity``; everything else -> ``equity`` (the common case here).
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
    # Bonds and precious metals are checked BEFORE broad commodity — TLT is not a commodity, GLD
    # is not a broad-basket commodity even though it was previously bucketed there.
    if s in _FIXED_INCOME_SYMBOLS:
        return "fixed_income"
    if s in _PRECIOUS_METALS_SYMBOLS:
        return "precious_metals"
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
        self._last_snapshot: IntelSnapshot | None = None
        self._ts = 0.0

    @property
    def last_snapshot(self) -> IntelSnapshot | None:
        """Last successfully-fetched snapshot, or None if no refresh has succeeded yet.

        Exposed so downstream consumers (interpret, agents, thesis alerters) can act on the
        exact same snapshot the overlay decided on — no second fetch, no drift between the
        decision layer and the reasoning layer.
        """
        return self._last_snapshot

    def refresh(self, *, force: bool = False) -> None:
        if not force and self._decisions is not None and (time.monotonic() - self._ts) < self._refresh:
            return
        try:
            snap = self._snapshot_fn()
            self._decisions = self._overlay.evaluate(snap)
            self._last_snapshot = snap
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


# ============================================================================================
# Persistence gate — cross-path tier 3
# ============================================================================================


# Class-to-domain mapping. Each overlay class is exposed to a subset of the graph's watched
# domains (energy / conflict / military / disaster / economy). The mapping mirrors the ``_compose``
# branches in :class:`~trading_live_claude.intel.overlay.RiskOverlay` — anything that shows up in
# a class's gate list is a candidate for the persistence gate. Ordering is not significant; the
# gate short-circuits on the first persistent domain it finds.
_CLASS_TO_DOMAINS: Mapping[OverlayClass, tuple[str, ...]] = {
    "equity":          ("conflict", "military"),
    "future":          ("conflict", "military", "energy"),
    "commodity":       ("energy", "conflict", "disaster"),
    "crypto":          ("conflict", "military"),
    "fx":              ("conflict", "energy", "economy"),
    "fixed_income":    ("economy", "conflict"),
    "precious_metals": ("conflict", "disaster", "energy"),
}


class PersistenceGate:
    """Callable ``(symbol) -> (halt: bool, reason: str)`` — refuses new entries when the symbol's
    overlay-class domains have been elevated across at least ``min_polls`` consecutive polls.

    Reads ``state/intel_graph.jsonl`` fresh on each poll (bounded — the file is append-only and
    the query is O(edges) per call). Refresh cadence caps the disk-read rate so a tight monitor
    loop doesn't re-read a hundred-thousand-edge file every 60 seconds; between refreshes the
    last computed decision is reused.

    A gate that cannot read the graph (missing file, parse error) fails OPEN — treats every
    symbol as clear. This matches the framework's rule that a broken intel path never causes an
    unexpected halt.

    Wire like :class:`OverlayProvider`::

        gate = PersistenceGate(min_polls=5, refresh_seconds=60.0)
        monitor = LiveMonitor(..., persistence_for=gate)
    """

    def __init__(self, *, min_polls: int = 5, refresh_seconds: float = 60.0,
                 graph_path: str | Path = DEFAULT_GRAPH_JOURNAL,
                 class_overrides: Mapping[str, OverlayClass] | None = None) -> None:
        self.min_polls = int(min_polls)
        self._refresh = float(refresh_seconds)
        self._graph_path = Path(graph_path)
        self._overrides = class_overrides
        # Precomputed per-domain persistence, refreshed on cadence. None → not yet read.
        self._persistence_by_domain: dict[str, int] | None = None
        self._ts = 0.0

    def refresh(self, *, force: bool = False) -> None:
        if not force and self._persistence_by_domain is not None and (
            time.monotonic() - self._ts
        ) < self._refresh:
            return
        try:
            edges = load_edges(self._graph_path)
            self._persistence_by_domain = {
                dom: edge_persistence(edges, predicate="elevated_in",
                                        object=("domain", dom))
                for doms in _CLASS_TO_DOMAINS.values() for dom in doms
            }
            self._ts = time.monotonic()
            log.info("persistence_gate.refreshed",
                     persistence=self._persistence_by_domain, min_polls=self.min_polls)
        except Exception as e:                          # fail-open: never halt on graph I/O errors
            log.warning("persistence_gate.refresh_failed", error=str(e))
            self._persistence_by_domain = {}

    def __call__(self, symbol: str) -> tuple[bool, str]:
        self.refresh()
        if not self._persistence_by_domain:
            return (False, "")
        cls = classify_symbol(symbol, self._overrides)
        domains = _CLASS_TO_DOMAINS.get(cls, ())
        # Halt if ANY of the class's exposed domains has persistently been elevated. Report the
        # domain with the longest run so the alert names the actual driver, not just "some domain".
        offenders = [(d, self._persistence_by_domain.get(d, 0)) for d in domains
                     if self._persistence_by_domain.get(d, 0) >= self.min_polls]
        if not offenders:
            return (False, "")
        driver, run = max(offenders, key=lambda p: p[1])
        return (True, f"{cls}: '{driver}' elevated {run} consecutive polls (>= {self.min_polls})")
