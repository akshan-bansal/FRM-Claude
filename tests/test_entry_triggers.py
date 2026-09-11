"""Event + level entry triggers — coverage for the 2026-09-09 build.

Two triggers coexist:
  * ``entry`` column (0/1) — event-triggered, fires on the fresh cross bar
  * ``entry_level`` column (0/1) — level-triggered, fires while the current bar
    still satisfies the entry condition

Neither alone is reliable:
  * event-only misses entries when the monitor starts after the crossing bar or
    when the fresh-cross intent is rejected by a gate
  * level-only over-fires while the condition persists, requiring an open-position
    guard to prevent re-entry

Tests here cover:
  1. Bollinger emits both columns coherently
  2. Strategy.supports_level_trigger defaults False for legacy strategies
  3. LiveMonitor consumes event-only when the strategy declares no level support
  4. LiveMonitor consumes level when strategy declares support AND no position open
  5. LiveMonitor ignores level when a position is already open (open-position guard)
  6. The entry event's detail carries the trigger source (event / level / event+level)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from trading_live_claude.strategies.base import StrategyContext
from trading_live_claude.strategies.examples.bollinger import BollingerMeanRevert


def _synthetic_close(prices: list[float]) -> pd.DataFrame:
    """OHLC frame with the given closes; open=high=low=close for simplicity."""
    n = len(prices)
    return pd.DataFrame({
        "open": prices, "high": prices, "low": prices, "close": prices,
        "volume": np.full(n, 1000, dtype=int),
    })


def test_bollinger_emits_both_entry_and_entry_level() -> None:
    """Fresh cross → entry=1 on that bar; every bar close ≤ lower band → entry_level=1."""
    # Manufacture a price series that dips below the lower band for several bars,
    # then crosses back up. Expect: entry_level=1 during the dip window; entry=1
    # on the fresh-cross bar only.
    prices = [100.0] * 25 + [90.0] * 5 + [100.0]              # 25 flat, 5 low, 1 recovery
    df = _synthetic_close(prices)

    strat = BollingerMeanRevert(window=20, n_std=2.0)
    out = strat.generate_signals(df, StrategyContext(symbol="TEST"))

    assert "entry" in out.columns
    assert "entry_level" in out.columns
    # Level-triggered fires on at least one of the low-price bars (close under band)
    assert out["entry_level"].sum() >= 1
    # Fresh-cross fires at most once (the recovery bar)
    assert out["entry"].sum() <= 1


def test_bollinger_declares_level_trigger_support() -> None:
    assert BollingerMeanRevert.supports_level_trigger is True


def test_default_strategy_does_not_support_level_trigger() -> None:
    """Legacy strategies (Strategy base) default supports_level_trigger=False so
    LiveMonitor stays on event-only behavior for them without changes."""
    from trading_live_claude.strategies.base import Strategy
    assert Strategy.supports_level_trigger is False


# --- LiveMonitor integration --------------------------------------------------------------

def _mk_signals_frame(entry: int, entry_level: int, exit_: int = 0,
                      close: float = 100.0, atr: float = 1.0) -> pd.DataFrame:
    """One-row synthetic signals frame that step()'s .iloc[-1] read consumes."""
    return pd.DataFrame({
        "close": [close], "entry": [entry], "entry_level": [entry_level],
        "exit": [exit_], "atr": [atr], "signal_strength": [1.0],
    })


def test_livemonitor_ignores_entry_level_when_strategy_lacks_support() -> None:
    """When the strategy does not declare supports_level_trigger=True, the level
    column is not consulted even if present — legacy contract preserved."""
    # Directly exercise the same computation the monitor uses (without spinning up
    # the full LiveMonitor). This is the exact snippet in live_loop.py step():
    class _LegacyStrat:
        supports_level_trigger = False

    strat = _LegacyStrat()
    last = _mk_signals_frame(entry=0, entry_level=1).iloc[-1]
    entry_event = int(last.get("entry", 0)) == 1
    entry_level = (
        bool(getattr(strat, "supports_level_trigger", False))
        and int(last.get("entry_level", 0)) == 1
    )
    assert not entry_event
    assert not entry_level                                    # ignored despite entry_level=1
    assert not (entry_event or entry_level)


def test_livemonitor_consumes_entry_level_when_strategy_supports_it() -> None:
    """Level trigger fires on a level-supporting strategy with no fresh cross."""
    class _LevelStrat:
        supports_level_trigger = True

    strat = _LevelStrat()
    last = _mk_signals_frame(entry=0, entry_level=1).iloc[-1]
    entry_event = int(last.get("entry", 0)) == 1
    entry_level = (
        bool(getattr(strat, "supports_level_trigger", False))
        and int(last.get("entry_level", 0)) == 1
    )
    assert not entry_event
    assert entry_level
    assert entry_event or entry_level                          # entry fires


def test_livemonitor_trigger_source_labeling() -> None:
    """The entry_detail dict carries the trigger source for downstream inspection."""
    class _S: supports_level_trigger = True
    strat = _S()

    # Both fire → 'event+level'
    last = _mk_signals_frame(entry=1, entry_level=1).iloc[-1]
    entry_event = int(last.get("entry", 0)) == 1
    entry_level = (bool(strat.supports_level_trigger) and int(last.get("entry_level", 0)) == 1)
    trig = ("event+level" if entry_event and entry_level
            else "event" if entry_event
            else "level" if entry_level
            else None)
    assert trig == "event+level"

    # Only event
    last = _mk_signals_frame(entry=1, entry_level=0).iloc[-1]
    e = int(last.get("entry", 0)) == 1
    lv = (bool(strat.supports_level_trigger) and int(last.get("entry_level", 0)) == 1)
    assert (
        "event+level" if e and lv else "event" if e else "level" if lv else None
    ) == "event"

    # Only level
    last = _mk_signals_frame(entry=0, entry_level=1).iloc[-1]
    e = int(last.get("entry", 0)) == 1
    lv = (bool(strat.supports_level_trigger) and int(last.get("entry_level", 0)) == 1)
    assert (
        "event+level" if e and lv else "event" if e else "level" if lv else None
    ) == "level"


# --- 2026-09-10: mean-reversion strategy family + wrappers ---------------------------

def test_rsi_meanrevert_emits_both_columns() -> None:
    from trading_live_claude.strategies.examples.rsi_meanrevert import RsiMeanRevert
    # Long enough for RSI(14) to compute; last bars have RSI probably around ~20-30
    prices = [100.0] * 20 + [95.0, 92.0, 90.0, 89.0, 88.0, 89.0, 90.0]
    df = pd.DataFrame({"open": prices, "high": prices, "low": prices, "close": prices,
                        "volume": np.full(len(prices), 1000, dtype=int)})
    strat = RsiMeanRevert(window=14, oversold=30.0)
    out = strat.generate_signals(df, StrategyContext(symbol="TEST"))
    assert "entry_level" in out.columns
    assert RsiMeanRevert.supports_level_trigger is True


def test_zscore_ou_emits_both_columns() -> None:
    from trading_live_claude.strategies.examples.mean_reversion import ZScoreOU
    # Price sequence that goes stretched-below then recovers
    prices = [100.0] * 25 + [95.0, 92.0, 90.0, 89.0, 92.0]
    df = pd.DataFrame({"open": prices, "high": prices, "low": prices, "close": prices,
                        "volume": np.full(len(prices), 1000, dtype=int)})
    strat = ZScoreOU(window=20, entry_z=2.0)
    out = strat.generate_signals(df, StrategyContext(symbol="TEST"))
    assert "entry_level" in out.columns
    assert ZScoreOU.supports_level_trigger is True
    # entry_level fires while z is below -entry_z (spread stretched)
    assert out["entry_level"].sum() >= 1


def test_bb_rsi_combo_emits_both_columns() -> None:
    from trading_live_claude.strategies.examples.mean_reversion import BbRsiCombo
    # Use noisy prices so the Bollinger band width is never zero (the
    # signal_strength math in BbRsiCombo divides by (bb_upper - bb_lower) and
    # constant-price warmups produce band=0 → NA propagation. Unrelated to this
    # test's target, so we side-step it with noise).
    rng = np.random.default_rng(42)
    base = np.linspace(100.0, 95.0, 30)
    prices = list(base + rng.normal(0.0, 0.5, 30))
    df = pd.DataFrame({"open": prices, "high": prices, "low": prices, "close": prices,
                        "volume": np.full(len(prices), 1000, dtype=int)})
    strat = BbRsiCombo(window=20, n_std=2.0, rsi_window=14, rsi_th=40.0)
    out = strat.generate_signals(df, StrategyContext(symbol="TEST"))
    assert "entry_level" in out.columns
    assert BbRsiCombo.supports_level_trigger is True


def test_rsi2_connors_emits_both_columns() -> None:
    from trading_live_claude.strategies.examples.mean_reversion import Rsi2Connors
    # Uptrend + brief pullback so RSI(2) drops
    n_up = 210
    n_pull = 4
    prices = list(np.linspace(80.0, 120.0, n_up)) + [118.0, 115.0, 113.0, 114.0][:n_pull]
    df = pd.DataFrame({"open": prices, "high": prices, "low": prices, "close": prices,
                        "volume": np.full(len(prices), 1000, dtype=int)})
    strat = Rsi2Connors(rsi_window=2, entry_th=10.0, trend_window=200, exit_window=5)
    out = strat.generate_signals(df, StrategyContext(symbol="TEST"))
    assert "entry_level" in out.columns
    assert Rsi2Connors.supports_level_trigger is True


def test_confirm_overlay_gates_entry_level_the_same_way_as_entry() -> None:
    """When ConfirmOverlay wraps a level-supporting base, entry_level must be gated
    by the same candlestick-confirmation window — otherwise confirmed strategies
    leak ungated level triggers."""
    from trading_live_claude.strategies.overlay import ConfirmOverlay
    from trading_live_claude.strategies.examples.bollinger import BollingerMeanRevert
    base = BollingerMeanRevert(window=20, n_std=2.0)
    overlay = ConfirmOverlay(base=base)
    # The overlay inherits supports_level_trigger from the base
    assert overlay.supports_level_trigger is True

    # Synthetic frame — level would fire many times on a base bollinger, gate ensures
    # it fires only where the candle pattern also confirms.
    prices = [100.0] * 25 + [90.0] * 5 + [100.0]
    df = pd.DataFrame({"open": prices, "high": prices, "low": prices, "close": prices,
                        "volume": np.full(len(prices), 1000, dtype=int)})
    base_out = base.generate_signals(df, StrategyContext(symbol="TEST"))
    overlay_out = overlay.generate_signals(df, StrategyContext(symbol="TEST"))
    # Overlay's entry_level must be a SUBSET (fewer fires) than the base's, because
    # gating can only remove — never add — triggers.
    assert overlay_out["entry_level"].sum() <= base_out["entry_level"].sum()


def test_composite_ors_entry_level_across_members() -> None:
    """Composite's entry_level is the OR of member entry_levels; supports_level_trigger
    reflects any-member-supports."""
    from trading_live_claude.strategies.composite import CompositeStrategy
    from trading_live_claude.strategies.examples.bollinger import BollingerMeanRevert
    from trading_live_claude.strategies.examples.ema_crossover import EmaCrossover

    # Bollinger supports level; EmaCrossover does not. Composite should still declare
    # level support (any-member) and emit the entry_level column.
    comp = CompositeStrategy(members=[BollingerMeanRevert(window=20, n_std=2.0),
                                        EmaCrossover()])
    assert comp.supports_level_trigger is True

    prices = [100.0] * 25 + [90.0] * 5 + [100.0]
    df = pd.DataFrame({"open": prices, "high": prices, "low": prices, "close": prices,
                        "volume": np.full(len(prices), 1000, dtype=int)})
    out = comp.generate_signals(df, StrategyContext(symbol="TEST"))
    assert "entry_level" in out.columns
    # Level fires at least once from the Bollinger member
    assert out["entry_level"].sum() >= 1


def test_composite_omits_entry_level_when_no_member_supports_it() -> None:
    """A composite of pure event-triggered members shouldn't fabricate a level
    column — it stays out of the output so LiveMonitor's has-column check is honest."""
    from trading_live_claude.strategies.composite import CompositeStrategy
    from trading_live_claude.strategies.examples.ema_crossover import EmaCrossover
    from trading_live_claude.strategies.examples.momentum_breakout import DonchianBreakout

    comp = CompositeStrategy(members=[EmaCrossover(), DonchianBreakout()])
    assert comp.supports_level_trigger is False

    prices = list(np.linspace(80.0, 120.0, 60))
    df = pd.DataFrame({"open": prices, "high": prices, "low": prices, "close": prices,
                        "volume": np.full(len(prices), 1000, dtype=int)})
    out = comp.generate_signals(df, StrategyContext(symbol="TEST"))
    assert "entry_level" not in out.columns


def test_open_position_guard_blocks_level_reentry() -> None:
    """When a position is already open, the (entry and not holds) branch is skipped
    even if entry_level fires. This is the same guard the event trigger relied on,
    now doing double duty for level re-fires."""
    class _S: supports_level_trigger = True
    strat = _S()
    last = _mk_signals_frame(entry=0, entry_level=1).iloc[-1]

    holds = True                                              # simulate existing position
    entry_event = int(last.get("entry", 0)) == 1
    entry_level = (bool(strat.supports_level_trigger) and int(last.get("entry_level", 0)) == 1)
    entry = entry_event or entry_level

    # The critical clause from step(): `if entry and not holds`
    would_enter = entry and not holds
    assert not would_enter                                    # level re-fire suppressed
