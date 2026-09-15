"""Tradeable quantity rules: lot step, minimum order size and minimum order value per symbol.

Sizing used to floor every position to whole units, so a $1,562 PAXG position (0.36 coins) became 0
and BTC could never trade on a $100k book. A ``QuantityRule`` rounds down to the venue's real step
instead — whole shares for stocks and contracts, the exchange-published lot for crypto.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class QuantityRule:
    step: float = 1.0
    min_qty: float = 0.0
    min_notional: float = 0.0

    @property
    def decimals(self) -> int:
        return max(0, -math.floor(math.log10(self.step))) if self.step < 1 else 0

    def floor(self, qty: float, price: float | None = None) -> float:
        """Largest tradeable quantity <= ``qty``; 0.0 when below the step, minimum size or minimum value."""
        if not math.isfinite(qty) or qty <= 0:
            return 0
        units = math.floor(qty / self.step + 1e-9)
        # Whole-unit steps keep returning ints so share/contract paths behave exactly as before.
        out: float = units * int(self.step) if float(self.step).is_integer() else round(units * self.step, self.decimals)
        if out <= 0 or out < self.min_qty:
            return 0
        if price is not None and self.min_notional and out * price < self.min_notional:
            return 0
        return out

    def why_zero(self, qty: float, price: float | None = None) -> str:
        if not math.isfinite(qty) or qty <= 0:
            return "no size before rounding"
        if math.floor(qty / self.step + 1e-9) == 0:
            return f"{qty:.8g} below one lot step {self.step:g}"
        if qty < self.min_qty:
            return f"{qty:.8g} below minimum order {self.min_qty:g}"
        if price is not None and self.min_notional and qty * price < self.min_notional:
            return f"value {qty * price:.2f} below minimum {self.min_notional:g}"
        return ""


WHOLE_UNITS = QuantityRule()


def whole_units(_symbol: str) -> QuantityRule:
    return WHOLE_UNITS


QuantityRuleFor = Callable[[str], QuantityRule]
