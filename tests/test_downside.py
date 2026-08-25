from __future__ import annotations

import pandas as pd

from trading_live_claude.signals.indicators import sma
from trading_live_claude.strategies import STRATEGIES
from trading_live_claude.strategies.base import StrategyContext

CTX = StrategyContext(symbol="T")


def test_mean_reversion_and_candlestick_carry_an_atr_stop() -> None:
    for name in ("bollinger", "rsi_meanrevert", "rsi2_connors", "zscore_ou", "bb_rsi_combo"):
        assert STRATEGIES[name]().stop_atr_mult == 3.0, name
    assert STRATEGIES["candle_hammer"]().stop_atr_mult == 3.0


def test_overlays_forward_the_base_stop() -> None:
    # confirm_* wraps a stopped mean-reversion base; the floor must survive the wrapper.
    assert STRATEGIES["confirm_bollinger"]().stop_atr_mult == 3.0
    assert STRATEGIES["confirm_rsi_meanrevert"]().stop_atr_mult == 3.0
    assert STRATEGIES["val_bollinger"]().stop_atr_mult == 3.0


def test_candlestick_exit_needs_a_prior_close_above_the_sma(random_walk_df: pd.DataFrame) -> None:
    """The fixed fade: a pattern firing at a low (price below the SMA) must not exit on the
    same bar. Exit may only fire where the prior close was at/above the SMA and now below it."""
    strat = STRATEGIES["candle_hammer"]()
    out = strat.generate_signals(random_walk_df, CTX)
    sma_exit = sma(out["close"], strat.exit_ma)
    prior_below = (out["close"] < sma_exit).shift(1).fillna(True)
    # No exit may occur on a bar whose prior close was below the SMA (can't fade a rise
    # that never happened) — this is exactly the same-bar exit the old code produced.
    assert not ((out["exit"] == 1) & prior_below).any()
