"""Candlestick confirmation overlay — a precision filter for the mean-reversion set.

``ConfirmOverlay`` wraps a base ``Strategy`` and *gates* its entries: a base entry
survives only if a bullish candlestick **reversal** pattern fired on the same bar or
within the prior ``lookback - 1`` bars. Everything else the base produces (``exit``,
``atr``, ``size_hint`` …) is passed through untouched, so the gated strategy flows
through the same backtest / risk / scoring pipeline as any other strategy.

Why only mean-reversion? Empirically (5y daily, 22-name TSX basket) candlestick
reversal confirmation is a genuine precision lever *at mean-reversion extremes* — it
lifted the robust bollinger sleeve's precision from ~0.32 to ~0.38, above the ~0.27
base rate — but it is flat-to-harmful on momentum/breakout sleeves, where a bullish
*reversal* candle contradicts a *continuation* entry. And within mean-reversion it only
pays on the *noisier* bases: it moved bollinger and rsi_meanrevert, but did nothing for
bases already selective on their own (zscore_ou, bb_rsi_combo sat at ~0.37 precision
either way) except discard trades. So only the two bases that measurably benefit are
wired as ready-made ``confirm_<base>`` strategies; ``ConfirmOverlay`` itself stays
general and can wrap any base on demand.

The gate is **past-only**: each detector references only current/earlier bars via
positive ``.shift(k)``, and the ``rolling(lookback).max()`` window looks strictly
backward, so gating adds no lookahead beyond the base strategy's own. Gating can only
remove entries, never add them, so the overlay's recall is <= the base's by
construction — the precision-stage counterpart to ``CompositeStrategy``'s recall union.
"""
from __future__ import annotations

from collections.abc import Callable

import pandas as pd

from ..signals.candlesticks import CANDLESTICK_PATTERNS
from .base import Strategy, StrategyContext
from .examples.bollinger import BollingerMeanRevert
from .examples.rsi_meanrevert import RsiMeanRevert

# Bullish reversal patterns used to confirm a mean-reversion entry. Deliberately the
# broad reversal set (not the rare 3-bar "strong" complexes): rare patterns almost
# never coincide with an already-sparse band touch, gating every trade away.
REVERSAL_CONFIRM: tuple[str, ...] = (
    "hammer",
    "inverted_hammer",
    "dragonfly_doji",
    "bullish_engulfing",
    "bullish_harami",
    "piercing_line",
    "tweezer_bottom",
    "belt_hold_bull",
)


class ConfirmOverlay(Strategy):
    """Gate a base strategy's entries on a bullish candlestick reversal confirmation."""

    name = "confirm_overlay"
    description = "Candlestick reversal confirmation gate on a base strategy's entries"

    def __init__(
        self,
        base: Strategy,
        patterns: tuple[str, ...] = REVERSAL_CONFIRM,
        lookback: int = 3,
    ) -> None:
        super().__init__(base=base.name, patterns=patterns, lookback=lookback)
        if lookback < 1:
            raise ValueError("lookback must be >= 1")
        unknown = set(patterns) - set(CANDLESTICK_PATTERNS)
        if unknown:
            raise ValueError(f"Unknown candlestick pattern(s): {sorted(unknown)}")
        if not patterns:
            raise ValueError("ConfirmOverlay needs at least one confirmation pattern")
        self.base = base
        self.patterns = tuple(patterns)
        self.lookback = lookback
        # Carry the base's opt-in exits through the wrapper (stop / trailing / time stop).
        self.stop_atr_mult = base.stop_atr_mult
        self.trail_atr_mult = base.trail_atr_mult
        self.time_stop_bars = base.time_stop_bars

    def required_history_bars(self) -> int:
        return self.base.required_history_bars()

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = self.base.generate_signals(df, ctx).copy()

        confirm = pd.Series(False, index=df.index)
        for pattern in self.patterns:
            confirm = confirm | CANDLESTICK_PATTERNS[pattern](df).astype(bool)
        # Fired on this bar or within the prior (lookback - 1) bars — backward-only.
        confirm_window = confirm.rolling(self.lookback, min_periods=1).max().astype(bool)

        base_entry = out["entry"].fillna(0).astype(bool)
        gated = base_entry & confirm_window
        out["entry"] = gated.astype(int)
        # Confirmation is a candidate feature: 1.0 where a gated entry survives, else
        # carry the base strength (zeroed on suppressed bars so the scorer sees the gate).
        if "signal_strength" in out.columns:
            out["signal_strength"] = out["signal_strength"].where(gated, 0.0)
        out["confirmed"] = gated.astype(int)
        return out


# Confirmation base factories, pre-loaded with the robust parameters found by the full
# optimization run (bollinger window=30/n_std=3.0 and rsi_meanrevert 14/35). Only these
# two mean-reversion bases measurably gained precision from the candlestick gate on the
# TSX basket, so only they are wired as ready-made confirm_<base> strategies. The advanced
# mean-reversion bases (rsi2_connors, zscore_ou, bb_rsi_combo) were tested and dropped —
# the gate only thinned their trades without lifting precision — but ConfirmOverlay can
# still wrap them explicitly if wanted.
CONFIRMED_BASES: dict[str, Callable[[], Strategy]] = {
    "bollinger": lambda: BollingerMeanRevert(window=30, n_std=3.0),
    "rsi_meanrevert": lambda: RsiMeanRevert(window=14, oversold=35),
}


def _make_confirm_strategy(base_name: str, factory: Callable[[], Strategy]) -> type[Strategy]:
    """Build a zero-arg ``confirm_<base>`` Strategy so it registers/tunes like any other."""

    class _ConfirmStrategy(ConfirmOverlay):
        name = f"confirm_{base_name}"
        description = f"{base_name} entries gated by bullish candlestick reversal confirmation"

        def __init__(self) -> None:
            super().__init__(base=factory())

    _ConfirmStrategy.__name__ = "Confirm" + base_name.title().replace("_", "")
    _ConfirmStrategy.__qualname__ = _ConfirmStrategy.__name__
    return _ConfirmStrategy


# One confirmation-gated strategy per confirmed base, keyed ``confirm_<base>``.
CONFIRM_STRATEGIES: dict[str, type[Strategy]] = {
    f"confirm_{name}": _make_confirm_strategy(name, factory)
    for name, factory in CONFIRMED_BASES.items()
}
