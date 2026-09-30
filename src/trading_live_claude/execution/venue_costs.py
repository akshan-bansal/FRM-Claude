"""What a fill actually costs, per venue — one source of truth for the gate and the paper fills.

The repo has carried a single number, ``$4.95 per fill``, everywhere: ``PaperBroker``'s default
commission, ``signals.profit_lock.round_trip_cost_frac``'s default, and the flat
``min_ticket_usd`` floor that stands in for "is this trade big enough to be worth doing". That
number is Questrade's equity commission and it is wrong on every other venue:

* **Questrade** — flat per fill (ETF buys are free; the flat rate is the conservative case).
* **Kraken** — a *percentage* of notional (taker ~0.26%, maker ~0.16%), no fixed floor. Charging
  $4.95 instead overstates a small crypto fill by an order of magnitude and understates a large
  one: the 2026-09-23 Kraken session paid a modelled $4.95 on a $184.97 LINK position (2.68% of
  notional) where Kraken would have charged about $0.48.
* **IB** — per *share* with a per-order minimum and a cap as a percentage of trade value, so cost
  per share falls as price rises and rises with share count. Neither a flat fee nor a flat rate.

``cost_for`` returns the dollar commission for one side. ``round_trip_cost_ratio`` expresses both
sides plus half-spread and slippage as a fraction of notional, which is the number a size gate
should test: a position is worth taking only when its costs are small relative to it.
"""
from __future__ import annotations

from dataclasses import dataclass

# Kraken's published taker fee at the lowest 30-day volume tier (the conservative side of
# maker/taker: a market order pays taker).
_KRAKEN_TAKER_BPS = 26.0
# IB US equities, tiered: per share, with a per-order minimum and a cap as a % of trade value.
_IB_PER_SHARE = 0.005
_IB_MIN_ORDER = 1.00
_IB_MAX_PCT_OF_NOTIONAL = 0.01


@dataclass(frozen=True)
class VenueCostModel:
    """Commission for one side of a fill. ``venue`` matches ``Broker.venue``."""

    venue: str = "questrade"
    flat_per_fill: float = 4.95          # questrade / paper
    commission_bps: float = 0.0          # kraken
    per_share: float = 0.0               # ib
    min_per_order: float = 0.0           # ib
    max_pct_of_notional: float = 0.0     # ib (0 = uncapped)
    slippage_bps: float = 5.0            # the paper broker's execution assumption
    half_spread_bps: float = 0.0         # venue-agnostic; pass the live spread when known

    @classmethod
    def for_venue(cls, venue: object, **overrides: float) -> VenueCostModel:
        """Model for a ``Broker.venue`` tag. Anything that is not a real string is treated as
        unknown and gets the conservative flat fee: a broker wrapper that answers every attribute
        through ``__getattr__`` (or a test double) must not be able to pick the cost model, which is
        the same trap ``Router._cost_reserve`` hit with ``commission_per_trade`` on 2026-09-25.
        """
        v = venue.lower() if isinstance(venue, str) and venue.strip() else "questrade"
        if v == "kraken":
            base = cls(venue=v, flat_per_fill=0.0, commission_bps=_KRAKEN_TAKER_BPS)
        elif v in {"ib", "ib_web"}:
            base = cls(venue=v, flat_per_fill=0.0, per_share=_IB_PER_SHARE,
                       min_per_order=_IB_MIN_ORDER, max_pct_of_notional=_IB_MAX_PCT_OF_NOTIONAL)
        else:
            # questrade, paper, global and anything unrecognised: the conservative flat fee. An
            # unknown venue must not look cheaper than the one venue we know charges a floor.
            base = cls(venue=v)
        if not overrides:
            return base
        return cls(**{**base.__dict__, **overrides})

    def cost_for(self, *, shares: float, price: float) -> float:
        """Dollar commission for one side of a fill of ``shares`` at ``price``."""
        notional = abs(shares) * price
        if notional <= 0:
            return 0.0
        cost = self.flat_per_fill + notional * self.commission_bps / 10_000.0
        if self.per_share > 0.0:
            cost += max(abs(shares) * self.per_share, self.min_per_order)
        if self.max_pct_of_notional > 0.0:
            cost = min(cost, notional * self.max_pct_of_notional)
        return cost

    def round_trip_cost_ratio(self, *, shares: float, price: float) -> float:
        """Both sides' cost as a fraction of notional: commission x2, spread and slippage x2."""
        notional = abs(shares) * price
        if notional <= 0:
            return 0.0
        commission = 2.0 * self.cost_for(shares=shares, price=price)
        friction = 2.0 * (self.half_spread_bps + self.slippage_bps) / 10_000.0
        return commission / notional + friction

    def min_notional_for(self, *, max_cost_ratio: float, price: float, shares: float) -> float:
        """Smallest notional whose round-trip cost fits ``max_cost_ratio`` at this price.

        Only meaningful where cost has a fixed component (a flat fee or an order minimum): with a
        pure bps venue the ratio is size-independent, and the answer is 0 when the frictions alone
        already fit. Returns ``inf`` when no size can fit, which is the honest answer for a
        ceiling tighter than the venue's spread and slippage.
        """
        friction = 2.0 * (self.half_spread_bps + self.slippage_bps) / 10_000.0
        headroom = max_cost_ratio - friction
        if headroom <= 0.0:
            return float("inf")
        fixed = 2.0 * (self.flat_per_fill + (self.min_per_order if self.per_share > 0 else 0.0))
        rate = 2.0 * self.commission_bps / 10_000.0
        if self.per_share > 0.0 and price > 0.0:
            rate += 2.0 * self.per_share / price
        if rate >= headroom:
            return float("inf")          # the rate alone breaches the ceiling at any size
        return fixed / (headroom - rate) if fixed > 0 else 0.0
