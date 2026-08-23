"""Candlestick-pattern strategies — patterns inform entry/exit.

Each bullish candlestick pattern becomes a long-only ``Strategy``: the pattern firing
is the **entry**; the **exit** is a momentum-fade (close below a short SMA). This wraps
the vectorized detectors in ``signals.candlesticks`` so every pattern flows through the
same backtest / signal-matrix / scoring / routing pipeline as any other strategy.

The detection is pure pandas over OHLC, so it runs on Questrade (QT) data directly; the
same pattern maps to QuantConnect's built-in ``CandlestickPatterns`` for LEAN deployment
(see ``integrations.lean_algorithm.render_candlestick_lean_algorithm``) — workable on
both venues.
"""
from __future__ import annotations

import pandas as pd

from ..signals.candlesticks import BULLISH_PATTERNS, CANDLESTICK_PATTERNS
from ..signals.indicators import atr, sma
from .base import Strategy, StrategyContext


class CandlestickStrategy(Strategy):
    """Long on a bullish candlestick pattern; exit on a short-SMA momentum fade."""

    name = "candlestick"
    description = "Candlestick-pattern entry, momentum-fade exit"

    def __init__(self, pattern: str = "hammer", exit_ma: int = 10, atr_window: int = 14) -> None:
        super().__init__(pattern=pattern, exit_ma=exit_ma, atr_window=atr_window)
        if pattern not in CANDLESTICK_PATTERNS:
            raise ValueError(f"Unknown candlestick pattern {pattern!r}")
        self.pattern = pattern
        self.exit_ma = exit_ma
        self.atr_window = atr_window

    def required_history_bars(self) -> int:
        return max(self.exit_ma * 4, 60)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        fires = CANDLESTICK_PATTERNS[self.pattern](out).astype(int)
        out["atr"] = atr(out, self.atr_window)
        out["entry"] = fires
        out["exit"] = (out["close"] < sma(out["close"], self.exit_ma)).astype(int)
        out["signal_strength"] = fires.astype(float)
        return out


def _make_candle_strategy(pattern: str) -> type[CandlestickStrategy]:
    """Build a zero-arg Strategy subclass for one pattern, registerable by name."""

    class _PatternStrategy(CandlestickStrategy):
        name = f"candle_{pattern}"

        def __init__(self) -> None:
            super().__init__(pattern=pattern)

    _PatternStrategy.__name__ = "Candle_" + pattern.title().replace("_", "")
    _PatternStrategy.__qualname__ = _PatternStrategy.__name__
    return _PatternStrategy


# One tradeable strategy per bullish pattern, keyed ``candle_<pattern>``.
CANDLE_STRATEGIES: dict[str, type[Strategy]] = {
    f"candle_{p}": _make_candle_strategy(p) for p in BULLISH_PATTERNS
}
