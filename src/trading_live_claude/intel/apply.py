"""Apply the WorldMonitor overlay to an existing allocation — de-risk only.

Given a book from :class:`trading_live_claude.portfolio.allocator.PortfolioAllocator` and the map
from each holding to its overlay asset class, scale every name's weight by that class's overlay
scalar. Because every scalar is ``<= 1``, the book can only shrink: the freed weight becomes cash,
never redistributed into another name. This is the whole point of a safety overlay — it stands the
book down, it never levers it up.
"""
from __future__ import annotations

from collections.abc import Mapping

from trading_live_claude.intel.overlay import OverlayClass, OverlayDecision
from trading_live_claude.portfolio.allocator import AllocationResult


def apply_overlay(allocation: AllocationResult, class_of: Mapping[str, OverlayClass],
                  decisions: Mapping[OverlayClass, OverlayDecision], *,
                  sleeves: Mapping[str, str] | None = None) -> AllocationResult:
    """Scale each holding by its asset class's overlay scalar; freed weight becomes cash.

    ``class_of`` maps holding name → overlay asset class. A name whose class is absent from
    ``decisions`` (or not in ``class_of``) is left unscaled. ``sleeves`` (name → sleeve) lets the
    sleeve breakdown be recomputed; without it the sleeve map is left empty.
    """
    new_weights: dict[str, float] = {}
    for name, w in allocation.weights.items():
        cls = class_of.get(name)
        scalar = decisions[cls].scalar if cls is not None and cls in decisions else 1.0
        nw = w * scalar
        if nw > 1e-9:
            new_weights[name] = nw

    gross = sum(new_weights.values())
    sleeve_w: dict[str, float] = {}
    if sleeves is not None:
        for name, wt in new_weights.items():
            s = sleeves.get(name, "default")
            sleeve_w[s] = sleeve_w.get(s, 0.0) + wt

    eff = _effective_positions(new_weights)
    return AllocationResult(weights=new_weights, gross_exposure=gross, cash=1.0 - gross,
                            sleeve_weights=sleeve_w, effective_positions=eff)


def _effective_positions(weights: dict[str, float]) -> float:
    total = sum(weights.values())
    if total <= 0:
        return 0.0
    ssq = sum((w / total) ** 2 for w in weights.values())
    return 1.0 / ssq if ssq > 0 else 0.0
