"""Tests for signals/profit_lock.py and its hooks in to_positions, the engine and the live loop."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.backtest.costs import CostModel
from trading_live_claude.backtest.engine import BacktestEngine
from trading_live_claude.monitor.live_loop import LiveMonitor, MonitorEvent
from trading_live_claude.signals.generator import SignalSet
from trading_live_claude.signals.profit_lock import ProfitLock, round_trip_cost_frac
from trading_live_claude.strategies.base import Strategy, StrategyContext

PL = ProfitLock()


# ---- the giveback curve ------------------------------------------------------------------------

def test_not_armed_below_one_atr() -> None:
    assert PL.giveback_atr(0.99) is None
    assert PL.stop_level(side=1, entry=100.0, extreme=100.9, atr=1.0) is None


def test_giveback_shrinks_on_the_log_curve() -> None:
    assert PL.giveback_atr(1.0) == pytest.approx(3.0)
    assert PL.giveback_atr(2.0) == pytest.approx(3.0 - 2.0 * np.log10(4.0), abs=1e-9)   # ~1.80
    assert PL.giveback_atr(3.0) == pytest.approx(3.0 - 2.0 * np.log10(7.0), abs=1e-9)   # ~1.31
    assert PL.giveback_atr(4.0) == pytest.approx(1.0)
    assert PL.giveback_atr(10.0) == pytest.approx(1.0)                                   # floored
    gb = [PL.giveback_atr(g) for g in (1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0)]
    assert all(b < a for a, b in pairwise(gb))                                           # tightens


def test_long_stop_is_floored_at_entry_just_after_arming() -> None:
    # +1 ATR: peak - 3 ATR = 98 < entry 100 -> floor binds at entry
    assert PL.stop_level(side=1, entry=100.0, extreme=101.0, atr=1.0) == pytest.approx(100.0)
    # +4 ATR: peak - 1 ATR = 103
    assert PL.stop_level(side=1, entry=100.0, extreme=104.0, atr=1.0) == pytest.approx(103.0)


def test_short_mirrors_long() -> None:
    assert PL.stop_level(side=-1, entry=100.0, extreme=99.5, atr=1.0) is None
    assert PL.stop_level(side=-1, entry=100.0, extreme=99.0, atr=1.0) == pytest.approx(100.0)
    assert PL.stop_level(side=-1, entry=100.0, extreme=96.0, atr=1.0) == pytest.approx(97.0)
    assert PL.hit(side=-1, price=97.0, entry=100.0, extreme=96.0, atr=1.0)
    assert not PL.hit(side=-1, price=96.9, entry=100.0, extreme=96.0, atr=1.0)


def test_unusable_inputs_never_lock() -> None:
    assert PL.stop_level(side=1, entry=100.0, extreme=110.0, atr=0.0) is None
    assert PL.stop_level(side=0, entry=100.0, extreme=110.0, atr=1.0) is None


def test_invalid_parameters_are_rejected() -> None:
    with pytest.raises(ValueError):
        ProfitLock(arm_atr=4.0, full_atr=1.0)
    with pytest.raises(ValueError):
        ProfitLock(giveback_min_atr=3.0, giveback_max_atr=1.0)
    with pytest.raises(ValueError):
        ProfitLock(cost_multiple=0.5)


# ---- transaction-cost cross-check --------------------------------------------------------------

def test_round_trip_cost_matches_the_cost_model_components() -> None:
    # $4.95 each side on $10,000 = 4.95 bps/side; 5 bps slippage; 2 bps half-spread given
    rt = round_trip_cost_frac(price=100.0, qty=100, half_spread_bps=2.0)
    assert rt == pytest.approx(2 * (4.95 / 10_000 + 7.0 / 10_000))
    # no live spread -> one tick over price, as CostModel.from_price does
    cm = CostModel.from_price(100.0, is_etf=True)
    rt2 = round_trip_cost_frac(price=100.0, qty=100, commission_per_fill=0.0)
    assert rt2 == pytest.approx(2 * cm.per_side_frac())
    assert round_trip_cost_frac(price=100.0, qty=0) == 0.0


def test_floor_is_net_breakeven_when_costs_are_given() -> None:
    rt = 0.002    # 20 bps round trip
    lvl = PL.stop_level(side=1, entry=100.0, extreme=101.0, atr=1.0, round_trip_cost_frac=rt)
    assert lvl == pytest.approx(100.0 * (1 + rt))       # never locks in a loss after costs
    lvl_s = PL.stop_level(side=-1, entry=100.0, extreme=99.0, atr=1.0, round_trip_cost_frac=rt)
    assert lvl_s == pytest.approx(100.0 * (1 - rt))


def test_lock_does_not_arm_until_the_gain_covers_the_cost_multiple() -> None:
    # 1 ATR = 0.3% of price, round trip 0.2% -> 2x costs = 0.4% > the 0.3% gain: stays unarmed
    assert PL.stop_level(side=1, entry=100.0, extreme=100.3, atr=0.3, round_trip_cost_frac=0.002) is None
    # at 0.5% the move pays for its exit twice over -> armed
    assert PL.stop_level(side=1, entry=100.0, extreme=100.5, atr=0.3, round_trip_cost_frac=0.002) is not None


# ---- backtest path -----------------------------------------------------------------------------

def _frame(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="D")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"close": c, "high": c + 0.5, "low": c - 0.5,
                         "entry": 0, "exit": 0, "atr": 1.0}, index=idx)


def _rise_then_fall() -> pd.DataFrame:
    # enter on bar 1 (signal on bar 0, shifted), run +5, then give it back
    closes = [100.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 104.5, 104.0, 103.8, 103.0, 102.0, 101.0]
    df = _frame(closes)
    df.iloc[0, df.columns.get_loc("entry")] = 1
    return df


def test_to_positions_exits_on_the_giveback_and_holds_without_the_lock() -> None:
    df = _rise_then_fall()
    plain = SignalSet(df).to_positions()
    locked = SignalSet(df).to_positions(profit_lock=PL)
    assert plain.iloc[-1] == 1                     # no strategy exit: held all the way down
    # peak 105 = +5 ATR -> giveback 1 ATR -> stop 104; the 104.0 close (bar 8) closes it
    assert locked.iloc[7] == 1 and locked.iloc[8] == 0
    assert (locked.iloc[8:] == 0).all()


def test_to_positions_profit_lock_has_no_lookahead() -> None:
    df = _rise_then_fall()
    full = SignalSet(df).to_positions(profit_lock=PL)
    for n in range(3, len(df) + 1):
        part = SignalSet(df.iloc[:n].copy()).to_positions(profit_lock=PL)
        pd.testing.assert_series_equal(part, full.iloc[:n], check_names=False)


def test_to_positions_computes_atr_when_the_strategy_omits_it() -> None:
    # Wilder ATR(14) needs history before the entry, so lead with 20 quiet bars (range 1.0).
    closes = [100.0] * 20 + [101.0, 102.0, 103.0, 104.0, 105.0, 104.5, 104.0, 103.0, 102.0, 101.0]
    df = _frame(closes).drop(columns=["atr"])
    df.iloc[19, df.columns.get_loc("entry")] = 1
    locked = SignalSet(df).to_positions(profit_lock=PL)
    plain = SignalSet(df).to_positions()
    assert plain.iloc[-1] == 1
    assert locked.iloc[-1] == 0                    # ATR was derived from high/low; the lock engaged
    # a frame with neither atr nor high/low cannot size the lock, so it stays inert
    bare = _frame(closes).drop(columns=["atr", "high", "low"])
    bare.iloc[19, bare.columns.get_loc("entry")] = 1
    assert SignalSet(bare).to_positions(profit_lock=PL).iloc[-1] == 1


def test_expensive_round_trip_keeps_the_lock_from_arming() -> None:
    df = _rise_then_fall()
    # a 6% round trip means the +5% peak never covers 2x costs -> no lock exit
    held = SignalSet(df).to_positions(profit_lock=PL, round_trip_cost_frac=0.06)
    assert held.iloc[-1] == 1


class _EntryOnce(Strategy):
    name = "entry_once"

    def required_history_bars(self) -> int:
        return 2

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        out["entry"] = 0
        out.iloc[0, out.columns.get_loc("entry")] = 1
        out["exit"] = 0
        out["atr"] = 1.0
        return out


def test_engine_passes_the_lock_and_its_own_costs_through() -> None:
    df = _rise_then_fall()[["close", "high", "low"]]
    eng = BacktestEngine(cost_model=CostModel.legacy(5.0))
    plain = eng.run(_EntryOnce(), df, "AAA")
    locked = eng.run(_EntryOnce(), df, "AAA", profit_lock=PL)
    assert plain.positions.iloc[-1] == 1 and locked.positions.iloc[-1] == 0
    assert locked.ending_equity > plain.ending_equity   # sold near 104 instead of riding to 101


# ---- live loop ---------------------------------------------------------------------------------

@dataclass
class _Quote:
    mid: float
    lastTradePrice: float
    bidPrice: float | None = None
    askPrice: float | None = None


@dataclass
class _Pos:
    symbol: str
    openQuantity: float
    averageEntryPrice: float
    currentPrice: float


class _Broker:
    name = "fake"

    def __init__(self) -> None:
        self.px = 100.0
        self.qty = 100.0

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        return 100_000.0

    def positions(self, account_number: str) -> list[_Pos]:
        return [_Pos("AAA", self.qty, 100.0, self.px)] if self.qty else []

    def quote(self, symbol: str) -> _Quote:
        return _Quote(mid=self.px, lastTradePrice=self.px, bidPrice=self.px - 0.01, askPrice=self.px + 0.01)


class _Market:
    def recent(self, symbol: str, bars: int, interval: str = "1d") -> pd.DataFrame:
        n = bars + 2
        return pd.DataFrame({"close": [100.0] * n, "high": [100.5] * n, "low": [99.5] * n})


class _Router:
    def __init__(self) -> None:
        self.intents: list[object] = []

    def submit(self, intent, **kw) -> None:
        self.intents.append(intent)


class _NoSignals(Strategy):
    name = "quiet"

    def required_history_bars(self) -> int:
        return 3

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        out["entry"] = 0
        out["exit"] = 0
        out["atr"] = 1.0
        return out


def _live(profit_lock: ProfitLock | None) -> tuple[LiveMonitor, _Broker, _Router, list[MonitorEvent]]:
    broker, router, events = _Broker(), _Router(), []
    mon = LiveMonitor(broker=broker, market=_Market(), strategy=_NoSignals(),   # type: ignore[arg-type]
                      sizer=None, router=router, account_number="X", symbols=["AAA"],  # type: ignore[arg-type]
                      on_event=events.append, emit_on_change_only=False, risk_model="atr",
                      heat_aggregation="sum", profit_lock=profit_lock)
    return mon, broker, router, events


def test_live_loop_locks_profit_after_the_giveback() -> None:
    mon, broker, router, events = _live(PL)
    for px in (101.0, 103.0, 105.0):          # run to +5 ATR, peak 105 -> lock at 104
        broker.px = px
        mon.step()
    assert not router.intents
    broker.px = 104.0
    mon.step()
    assert len(router.intents) == 1
    ev = events[-1]
    assert ev.kind == "exit" and ev.detail["reason"] == "profit_lock"
    assert ev.detail["lock_level"] == pytest.approx(104.0)
    assert ev.detail["peak"] == pytest.approx(105.0)
    assert 0 < ev.detail["round_trip_cost_frac"] < 0.01


def test_live_loop_is_unchanged_with_the_lock_off() -> None:
    mon, broker, router, events = _live(None)
    for px in (101.0, 103.0, 105.0, 104.0, 101.0):
        broker.px = px
        mon.step()
    assert not router.intents
    assert all(e.kind == "hold" for e in events)


# ---- re-entry lockout (level-style entries) ----------------------------------------------------

def test_level_entry_cannot_buy_straight_back_after_a_lock_exit() -> None:
    df = _rise_then_fall()
    df["entry"] = 1                                # level-style: "on" every bar, like ts_momentum
    locked = SignalSet(df).to_positions(profit_lock=PL)
    plain = SignalSet(df).to_positions()
    assert (plain.iloc[1:] == 1).all()
    assert locked.iloc[8] == 0 and (locked.iloc[8:] == 0).all()   # sold at 104 and stays out


def test_lockout_clears_once_the_entry_signal_resets() -> None:
    df = _rise_then_fall()
    df["entry"] = 1
    df = pd.concat([df, _frame([101.0, 101.0, 101.0])])
    df.index = pd.date_range("2024-01-01", periods=len(df), freq="D")
    df.iloc[-3, df.columns.get_loc("entry")] = 0   # signal switches off for one bar...
    df.iloc[-2, df.columns.get_loc("entry")] = 1   # ...then fires again: a fresh entry
    df.iloc[-1, df.columns.get_loc("entry")] = 1
    pos = SignalSet(df).to_positions(profit_lock=PL)
    assert pos.iloc[-4] == 0                       # still locked out while the old signal held
    assert pos.iloc[-1] == 1                       # re-entered on the fresh signal (shifted a bar)


class _LevelEntry(_NoSignals):
    name = "level"

    def __init__(self) -> None:
        super().__init__()
        self.entry_flag = 1

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = super().generate_signals(df, ctx)
        out["entry"] = self.entry_flag
        return out


def test_live_loop_does_not_rebuy_into_the_signal_it_just_sold() -> None:
    broker, router, events = _Broker(), _Router(), []
    strat = _LevelEntry()
    mon = LiveMonitor(broker=broker, market=_Market(), strategy=strat,       # type: ignore[arg-type]
                      sizer=None, router=router, account_number="X", symbols=["AAA"],  # type: ignore[arg-type]
                      on_event=events.append, emit_on_change_only=False, risk_model="atr",
                      heat_aggregation="sum", profit_lock=PL)
    for px in (103.0, 105.0, 104.0):              # run to 105, lock at 104 -> sell
        broker.px = px
        mon.step()
    assert len(router.intents) == 1 and events[-1].detail["reason"] == "profit_lock"
    broker.qty = 0.0                              # the sell filled
    mon.step()                                    # entry still on: must NOT re-buy (sizer is None)
    assert len(router.intents) == 1
    assert events[-1].kind == "hold" and events[-1].detail == {"profit_lock_cooldown": True}
    strat.entry_flag = 0
    mon.step()                                    # signal resets -> lockout clears
    assert "AAA" not in mon._pl_lockout


def test_exempt_strategy_is_never_locked() -> None:
    broker, router, events = _Broker(), _Router(), []
    mon = LiveMonitor(broker=broker, market=_Market(), strategy=_NoSignals(),   # type: ignore[arg-type]
                      sizer=None, router=router, account_number="X", symbols=["AAA"],  # type: ignore[arg-type]
                      on_event=events.append, emit_on_change_only=False, risk_model="atr",
                      heat_aggregation="sum", profit_lock=PL, profit_lock_exempt={"quiet"})
    for px in (103.0, 105.0, 104.0, 101.0):       # would lock at 104 if the strategy weren't exempt
        broker.px = px
        mon.step()
    assert not router.intents
    assert all(e.kind == "hold" for e in events)
