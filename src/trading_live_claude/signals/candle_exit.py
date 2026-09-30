"""Candlestick exit — exit Variant #2: sell a winner when a bearish reversal candle completes.

The entry side already uses candlesticks (``strategies.overlay.ConfirmOverlay`` gates the
mean-reversion entries on a bullish reversal). Exits never did. This closes a long position that is
**in profit** when a bearish reversal pattern completes, on the bar after it, which is the same
"trade on the next bar" timing as strategy exits.

* **Patterns:** the bearish mirrors of the eight bullish ``REVERSAL_CONFIRM`` patterns (hammer →
  hanging man, inverted hammer → shooting star, dragonfly → gravestone doji, bullish → bearish
  engulfing and harami, piercing line → dark cloud cover, tweezer bottom → top, bull → bear belt
  hold). Past-only detectors from ``signals.candlesticks``.
* **Only on winners:** it arms once the position is up ``min_gain_atr`` ATRs (ATR at entry) as of
  the pattern bar's close. A reversal candle under a losing position is left to the strategy
  exit and the stops.
* **Transaction-cost cross-check:** it also needs the gain to cover ``cost_multiple`` round-trip
  costs, the same rule as Variant #1, so it never "takes" a profit that costs would eat.
* **Re-entry lockout:** after a candle exit the side stays blocked until its entry signal switches
  off (shared with Variant #1 in ``SignalSet.to_positions`` and ``LiveMonitor``).

Long-only (the live loop only opens longs). Parameters fixed a priori (2026-09-18), not fitted.
Off by default everywhere.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .candlesticks import CANDLESTICK_PATTERNS

BEARISH_REVERSAL: tuple[str, ...] = (
    "hanging_man",
    "shooting_star",
    "gravestone_doji",
    "bearish_engulfing",
    "bearish_harami",
    "dark_cloud_cover",
    "tweezer_top",
    "belt_hold_bear",
)


@dataclass(frozen=True)
class WinnerGate:
    """Shared arming rule for the completed-bar signal exits (Variant #2 candle, #3 overbought):
    act only on a long that is up ``min_gain_atr`` ATRs (ATR at entry) and whose gain covers
    ``cost_multiple`` round-trip costs, as of the signal bar's close."""

    min_gain_atr: float = 1.0
    cost_multiple: float = 2.0

    def __post_init__(self) -> None:
        if self.min_gain_atr < 0:
            raise ValueError(f"min_gain_atr must be >= 0, got {self.min_gain_atr}")
        if self.cost_multiple < 1.0:
            raise ValueError(f"cost_multiple must be >= 1, got {self.cost_multiple}")

    def fired(self, df: pd.DataFrame) -> pd.DataFrame:     # pragma: no cover - overridden
        raise NotImplementedError

    def any_fired(self, df: pd.DataFrame) -> pd.Series:
        f = self.fired(df)
        if f.empty:
            return pd.Series(False, index=df.index)
        return f.any(axis=1)

    def armed(self, *, entry: float, ref_price: float, atr: float,
              round_trip_cost_frac: float = 0.0) -> bool:
        """True if a long bought at ``entry`` is far enough in profit at ``ref_price`` to act."""
        if entry <= 0 or atr <= 0:
            return False
        gain = ref_price - entry
        if gain < self.min_gain_atr * atr:
            return False
        rt = max(float(round_trip_cost_frac), 0.0)
        return not (rt > 0 and gain / entry < self.cost_multiple * rt)


@dataclass(frozen=True)
class CandleExit(WinnerGate):
    patterns: tuple[str, ...] = BEARISH_REVERSAL

    def __post_init__(self) -> None:
        super().__post_init__()
        unknown = [p for p in self.patterns if p not in CANDLESTICK_PATTERNS]
        if unknown:
            raise ValueError(f"unknown candlestick patterns: {unknown}")

    def fired(self, df: pd.DataFrame) -> pd.DataFrame:
        """Boolean frame, one column per pattern, True on the bar the pattern completes.

        Needs open/high/low/close. Every detector is past-only (current and earlier bars).
        """
        if not {"open", "high", "low", "close"}.issubset(df.columns):
            return pd.DataFrame(index=df.index)
        return pd.DataFrame({p: CANDLESTICK_PATTERNS[p](df).astype(bool) for p in self.patterns},
                            index=df.index)
