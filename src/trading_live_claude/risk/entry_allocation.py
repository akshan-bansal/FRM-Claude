"""Parallel entry allocation: size every entry of one poll against the same book, jointly.

The live loop used to size and route entries one symbol at a time, in watchlist order. Each intent
was sized on the full equity, so the first names in the list took the leverage headroom and the
position slots, and later names were trimmed to scraps or rejected (2026-09-22: RSI.TO trimmed
from 1863 shares to 4 by the leverage cap, then rejected at the position cap, on every poll). The
position count handed to the Router was also the poll-start snapshot, so four entries in one poll
could open four positions against a cap of three.

This module decides the whole poll's entries at once, before any is routed:

1. **Slots.** New positions are ranked by conviction (then symbol, for a stable order) and only as
   many as the open-position cap has room for are kept. An add to a position already held takes
   no slot.
2. **Per-symbol cap.** Each candidate is clipped to its own per-symbol notional headroom.
3. **Shared budget.** If the clipped candidates together exceed the gross-leverage headroom, every
   one is scaled by the same factor (pro rata), so no name is favoured by its place in the list.
4. **Rounding.** Shares are floored to the venue's quantity rule; a candidate that rounds to zero
   is dropped with a reason.

It never weakens a gate: the Router still gates and trims every intent this produces. The caps
are read from the Router so the two agree, and a Router without them leaves that step unbounded.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .quantity import WHOLE_UNITS, QuantityRule


@dataclass(frozen=True)
class EntryCandidate:
    symbol: str
    shares: float                  # what the sizer proposed on its own
    price: float
    conviction: float              # ranking key for position slots
    cap_notional: float = math.inf  # per-symbol notional headroom (cap minus already held)
    new_position: bool = True      # False = add to a held position (takes no slot)
    qty_rule: QuantityRule = WHOLE_UNITS


@dataclass(frozen=True)
class Allocation:
    symbol: str
    shares: float
    proposed_shares: float
    scale: float                   # budget scale applied (1.0 = none)
    dropped: str = ""              # non-empty = not routed, and why

    @property
    def routed(self) -> bool:
        return not self.dropped and self.shares > 0


def allocate_entries(
    candidates: list[EntryCandidate], *, slots: int | None, budget: float,
) -> list[Allocation]:
    """Allocate one poll's entries jointly. Returns one ``Allocation`` per candidate, in rank order."""
    ranked = sorted(candidates, key=lambda c: (-c.conviction, c.symbol))
    n_new = sum(1 for c in ranked if c.new_position)
    kept: list[EntryCandidate] = []
    out: dict[str, Allocation] = {}
    rank = 0
    for c in ranked:
        if c.new_position:
            rank += 1
            if slots is not None and rank > max(slots, 0):
                out[c.symbol] = Allocation(c.symbol, 0, c.shares, 0.0,
                                           dropped=f"no position slot: ranked {rank} of {n_new}, "
                                                   f"{max(slots, 0)} free")
                continue
        kept.append(c)

    want = {c.symbol: max(0.0, min(c.shares * c.price, c.cap_notional)) for c in kept}
    total = sum(want.values())
    budget = max(budget, 0.0)
    scale = 1.0 if total <= budget or total <= 0 else budget / total
    for c in kept:
        shares = c.qty_rule.floor(want[c.symbol] * scale / c.price, c.price) if c.price > 0 else 0
        if shares <= 0:
            out[c.symbol] = Allocation(c.symbol, 0, c.shares, scale,
                                       dropped=f"rounds to 0 after allocation "
                                               f"(${want[c.symbol] * scale:,.0f} at ${c.price:,.2f})")
        else:
            out[c.symbol] = Allocation(c.symbol, shares, c.shares, scale)
    return [out[c.symbol] for c in ranked]
