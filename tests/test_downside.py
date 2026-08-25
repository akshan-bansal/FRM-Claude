from __future__ import annotations

import pandas as pd

from trading_live_claude.signals.indicators import sma
from trading_live_claude.strategies import STRATEGIES
from trading_live_claude.strategies.base import StrategyContext
from trading_live_claude.strategies.examples.bollinger import BollingerMeanRevert
from trading_live_claude.strategies.overlay import ConfirmOverlay

CTX = StrategyContext(symbol="T")


def test_stop_is_opt_in_for_mean_reversion() -> None:
    # The test showed a 3x ATR stop is neutral-to-harmful on mean-reversion, so it is
    # off by default; users opt in by setting stop_atr_mult on the instance.
    for name in ("bollinger", "rsi_meanrevert", "rsi2_connors", "zscore_ou", "bb_rsi_combo"):
        assert STRATEGIES[name]().stop_atr_mult is None, name
    s = STRATEGIES["bollinger"]()
    s.stop_atr_mult = 2.0
    assert s.stop_atr_mult == 2.0  # opting in is respected (engine reads this attr)


def test_bollinger_carries_the_tested_time_stop() -> None:
    # A 15-bar time stop tested as a clean win on bollinger, so it's on by default there.
    assert STRATEGIES["bollinger"]().time_stop_bars == 15
    # rsi keeps no time stop (it was mixed for rsi in the test).
    assert STRATEGIES["rsi_meanrevert"]().time_stop_bars is None


def test_candlestick_keeps_a_stop_as_its_only_downside_exit() -> None:
    # The fade exit only fires after a rise above the SMA, so a stop is candlestick's
    # sole floor for a trade that never recovers — kept on by design.
    assert STRATEGIES["candle_hammer"]().stop_atr_mult == 3.0


def test_overlays_forward_base_exits() -> None:
    base = BollingerMeanRevert()
    base.stop_atr_mult = 2.5
    base.trail_atr_mult = 3.0
    ov = ConfirmOverlay(base)
    assert ov.stop_atr_mult == 2.5 and ov.trail_atr_mult == 3.0
    # Registered confirm_bollinger inherits bollinger's defaults: no ATR stop, time stop 15.
    assert STRATEGIES["confirm_bollinger"]().stop_atr_mult is None
    assert STRATEGIES["confirm_bollinger"]().time_stop_bars == 15


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
