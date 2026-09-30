"""Profit-lock ratchet — an exit that gets more sensitive the further a position runs in profit.

Strategy exits say when the *thesis* is over. This rule says when enough of a *gain* has been
given back. It does nothing until a position is up ``arm_atr`` ATRs from entry. From then on it
exits when price falls back from the best level reached since entry by more than an allowed
giveback, and that allowance shrinks as the gain grows:

    gain      = (peak - entry) / ATR_at_entry                      (in ATRs)
    giveback  = max_gb - (max_gb - min_gb) * log_ramp(gain; arm_atr, full_atr)
    stop      = peak - giveback * ATR_at_entry, never below net breakeven once armed

Defaults (arm 1 ATR, full 4 ATR, giveback 3 -> 1 ATR): at +1 ATR the allowance is 3.0 ATR (so the
breakeven floor binds), at +2 ATR ~1.8, at +3 ATR ~1.3, from +4 ATR on 1.0. The log curve front-loads
the tightening: most of it happens in the first ATR or two past arming.

**Transaction-cost cross-check.** Locking a gain means paying a round trip. With
``round_trip_cost_frac`` supplied (commission + slippage + half-spread, both sides, as a fraction
of entry notional; see :func:`round_trip_cost_frac`):

* the floor is **net** breakeven, ``entry * (1 + cost)``, not bare entry, so a lock exit never
  books a loss after costs;
* the lock only arms once the gain at the peak is at least ``cost_multiple`` round trips
  (default 2), so it never "locks" a move that costs would eat.

Shorts mirror it (trough instead of peak, stop above, capped at entry). Pure functions only: the
backtest (:meth:`signals.generator.SignalSet.to_positions`) and the live loop
(:class:`monitor.live_loop.LiveMonitor`) call the same :meth:`ProfitLock.stop_level`, so the rule
that is backtested is the rule that trades.

Parameters are fixed a priori (2026-09-18), not fitted. Off by default everywhere.

**Status: parked as exit Variant #1 (2026-09-18).** Kept for comparison against later exit variants;
not enabled in any session. Backtest evidence and the revival checklist are in NEXT_SESSION.md
under "Exit variants — parked for comparison".
"""
from __future__ import annotations

from dataclasses import dataclass

from trading_live_claude.curves import log_ramp


@dataclass(frozen=True)
class ProfitLock:
    arm_atr: float = 1.0            # gain (in ATRs) before the lock engages
    full_atr: float = 4.0           # gain at which the giveback reaches its minimum
    giveback_max_atr: float = 3.0   # allowed retrace from the peak just after arming
    giveback_min_atr: float = 1.0   # allowed retrace from the peak once fully tightened
    floor_at_entry: bool = True     # once armed, never let a winner close below net breakeven
    cost_multiple: float = 2.0      # peak gain must be >= this many round-trip costs to arm

    def __post_init__(self) -> None:
        if not (0 < self.arm_atr < self.full_atr):
            raise ValueError(f"need 0 < arm_atr < full_atr, got {self.arm_atr}, {self.full_atr}")
        if self.cost_multiple < 1.0:
            raise ValueError(f"cost_multiple must be >= 1 (a lock below one round trip loses money), "
                             f"got {self.cost_multiple}")
        if not (0 < self.giveback_min_atr <= self.giveback_max_atr):
            raise ValueError(f"need 0 < giveback_min_atr <= giveback_max_atr, got "
                             f"{self.giveback_min_atr}, {self.giveback_max_atr}")

    def giveback_atr(self, gain_atr: float) -> float | None:
        """Allowed retrace from the peak, in ATRs, or None while the lock is not armed."""
        if gain_atr < self.arm_atr:
            return None
        t = log_ramp(gain_atr, self.arm_atr, self.full_atr)
        return self.giveback_max_atr - (self.giveback_max_atr - self.giveback_min_atr) * t

    def stop_level(self, *, side: int, entry: float, extreme: float, atr: float,
                   round_trip_cost_frac: float = 0.0) -> float | None:
        """Exit level for a position, or None if the lock is not armed (or inputs are unusable).

        ``side`` is +1 long / -1 short, ``extreme`` the best price since entry (peak for a long,
        trough for a short), ``atr`` the ATR at entry, ``round_trip_cost_frac`` both sides' costs
        as a fraction of entry notional (0 = cost-blind).
        """
        if atr <= 0 or entry <= 0 or side not in (1, -1):
            return None
        rt = max(float(round_trip_cost_frac), 0.0)
        gain_frac = (extreme - entry) / entry if side == 1 else (entry - extreme) / entry
        if rt > 0 and gain_frac < self.cost_multiple * rt:
            return None                     # cost cross-check: the move doesn't yet pay for its exit
        gain_atr = (extreme - entry) / atr if side == 1 else (entry - extreme) / atr
        gb = self.giveback_atr(gain_atr)
        if gb is None:
            return None
        if side == 1:
            level = extreme - gb * atr
            return max(level, entry * (1.0 + rt)) if self.floor_at_entry else level
        level = extreme + gb * atr
        return min(level, entry * (1.0 - rt)) if self.floor_at_entry else level

    def hit(self, *, side: int, price: float, entry: float, extreme: float, atr: float,
            round_trip_cost_frac: float = 0.0) -> bool:
        level = self.stop_level(side=side, entry=entry, extreme=extreme, atr=atr,
                                round_trip_cost_frac=round_trip_cost_frac)
        if level is None:
            return False
        return price <= level if side == 1 else price >= level


def round_trip_cost_frac(
    *, price: float, qty: float, commission_per_fill: float = 4.95, slippage_bps: float = 5.0,
    half_spread_bps: float | None = None, tick: float = 0.01,
) -> float:
    """Both sides' transaction cost as a fraction of position notional.

    Mirrors :class:`backtest.costs.CostModel`: a fixed commission per fill (the paper broker
    charges $4.95), slippage, and half the bid-ask spread per side. With no live spread
    ``half_spread_bps`` falls back to one tick over price, as ``CostModel.from_price`` does.
    """
    notional = abs(qty) * price
    if notional <= 0 or price <= 0:
        return 0.0
    hs = half_spread_bps if half_spread_bps is not None else (0.5 * tick / price) * 10_000.0
    per_side = commission_per_fill / notional + (slippage_bps + hs) / 10_000.0
    return 2.0 * per_side
