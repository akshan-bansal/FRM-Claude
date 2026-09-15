"""Sizing v2: the correctness fixes the live loop applies when a ``SizingPolicy`` is supplied.

Without a policy the monitor sizes exactly as before (whole units, 252-day annualization, no stop
enforcement), so running sessions and backtests are unchanged until a launch opts in:

  * ``quantity_rule_for`` — venue lot step / minimum size / minimum value instead of whole units.
  * ``periods_per_year_for`` — set by the launch per venue: 365 on Kraken, 252 on Questrade and IB.
  * ``enforce_stops`` — exit through the router when price trades through the sized ATR stop; the
    sizer's vol-target path assumes that stop exists, so without it the risk budget is fiction.
  * ``journal_path`` — one JSON line per entry decision, so a zero-size or a collapsed conviction
    is visible after the fact instead of silently producing no orders.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .quantity import QuantityRule, whole_units

TRADING_DAYS = 252.0
CALENDAR_DAYS = 365.0


def trading_days(_symbol: str) -> float:
    """Questrade equities and IB futures: daily bars exist on exchange sessions only."""
    return TRADING_DAYS


def calendar_days(_symbol: str) -> float:
    """Kraken: bars print every calendar day, so a year of daily returns is 365 of them."""
    return CALENDAR_DAYS


@dataclass
class SizingPolicy:
    quantity_rule_for: Callable[[str], QuantityRule] = whole_units
    periods_per_year_for: Callable[[str], float] = trading_days
    enforce_stops: bool = True
    journal_path: Path | None = None
    session_id: str | None = None
    _stops: dict[str, float] = field(default_factory=dict, repr=False)

    def record_stop(self, symbol: str, stop: float) -> None:
        self._stops[symbol] = stop

    def stop_for(self, symbol: str, *, avg_entry: float, atr_value: float, atr_multiple: float) -> float | None:
        """The stop recorded at entry; after a restart, rebuild it from the average entry price."""
        if symbol in self._stops:
            return self._stops[symbol]
        if avg_entry > 0 and atr_value > 0:
            self._stops[symbol] = avg_entry - atr_multiple * atr_value
            return self._stops[symbol]
        return None

    def clear_stop(self, symbol: str) -> None:
        self._stops.pop(symbol, None)

    def journal(self, row: dict[str, Any]) -> None:
        if self.journal_path is None:
            return
        rec = {"ts": datetime.now(UTC).isoformat(), "session_id": self.session_id, **row}
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
