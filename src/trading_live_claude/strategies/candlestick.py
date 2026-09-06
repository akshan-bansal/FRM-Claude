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
    stop_atr_mult: float | None = 3.0  # floor each trade if it never recovers above the SMA

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
        # Momentum fade: exit only once price has risen back above the short SMA and then
        # closed below it. A bullish pattern fires at a low (price already below the SMA),
        # so the old ``close < SMA`` test exited on the entry bar itself — this requires the
        # up-move to happen first, and the ATR stop floors trades that never recover.
        close = out["close"]
        sma_exit = sma(close, self.exit_ma)
        was_above = (close >= sma_exit).shift(1).fillna(False)
        out["exit"] = (was_above & (close < sma_exit)).astype(int)
        out["signal_strength"] = fires.astype(float)
        return out


def _make_candle_strategy(pattern: str) -> type[CandlestickStrategy]:
    """Build a zero-arg Strategy subclass for one pattern, registerable by name."""

    class _PatternStrategy(CandlestickStrategy):
        name = f"candle_{pattern}"

        def __init__(self, exit_ma: int = 10, atr_window: int = 14) -> None:
            super().__init__(pattern=pattern, exit_ma=exit_ma, atr_window=atr_window)

    _PatternStrategy.__name__ = "Candle_" + pattern.title().replace("_", "")
    _PatternStrategy.__qualname__ = _PatternStrategy.__name__
    return _PatternStrategy


# One tradeable strategy per bullish pattern, keyed ``candle_<pattern>``.
CANDLE_STRATEGIES: dict[str, type[Strategy]] = {
    f"candle_{p}": _make_candle_strategy(p) for p in BULLISH_PATTERNS
}
