"""Transaction-cost model for the backtest engine.

Every score in the validated pool has been *gross* of cost: the engine applied a fixed slippage
but never charged the commission it declared, and modelled no bid-ask spread. This makes those
costs explicit and configurable so a backtest (and the walk-forward built on it) can be run **net
of cost** — which matters most exactly where the pool has fished: low-priced names (wide relative
spreads) and small tickets (the commission floor dominates).

Costs are expressed as **per-side** fractions of traded notional and charged on each position
transition (a round-trip pays twice; a long↔short flip pays on both legs, which the engine's
``|position.diff()|`` already counts). Three components:

* ``commission_bps`` — brokerage commission as a rate (Questrade ETFs are free; US equities carry
  a per-share fee with a \$4.95 floor, which is a *rate* only relative to ticket size).
* ``slippage_bps`` — execution slippage vs the decision price.
* ``half_spread_bps`` — crossing half the bid-ask spread; price-dependent (a \$0.01 tick is ~6 bps
  on an \$8 stock but ~0.07 bps on QQQ), so :meth:`from_price` derives it from tick / price.

A fixed ``commission_per_trade`` (dollars) can be added, converted to a rate against
``notional_per_trade`` — the fully-invested engine trades ~account-notional per position.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    commission_bps: float = 0.0
    slippage_bps: float = 5.0            # the engine's historical default (slippage only)
    half_spread_bps: float = 0.0
    commission_per_trade: float = 0.0    # fixed $ per side
    notional_per_trade: float = 100_000.0

    @property
    def per_side_bps(self) -> float:
        return self.commission_bps + self.slippage_bps + self.half_spread_bps

    def per_side_frac(self) -> float:
        """Per-side cost as a fraction of notional (bps components + fixed \$ / notional)."""
        fixed = self.commission_per_trade / self.notional_per_trade if self.notional_per_trade > 0 else 0.0
        return self.per_side_bps / 10_000.0 + fixed

    # ---- presets ---------------------------------------------------------
    @classmethod
    def frictionless(cls) -> CostModel:
        return cls(commission_bps=0.0, slippage_bps=0.0, half_spread_bps=0.0)

    @classmethod
    def legacy(cls, slippage_bps: float = 5.0) -> CostModel:
        """The engine's original behaviour: slippage only, no commission or spread."""
        return cls(slippage_bps=slippage_bps)

    @classmethod
    def from_price(cls, price: float, *, is_etf: bool, tick: float = 0.01, slippage_bps: float = 5.0,
                   equity_commission_bps: float = 2.0) -> CostModel:
        """A realistic per-name model: half-spread from tick/price, plus slippage and (for equities)
        a commission rate. ETFs are treated as commission-free (Questrade buys are free)."""
        half_spread_bps = (0.5 * tick / price) * 10_000.0 if price > 0 else 0.0
        commission_bps = 0.0 if is_etf else equity_commission_bps
        return cls(commission_bps=commission_bps, slippage_bps=slippage_bps, half_spread_bps=half_spread_bps)
