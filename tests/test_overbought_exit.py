"""Tests for signals/overbought_exit.py (exit Variant #3) and its hooks in to_positions and the live loop."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.monitor.live_loop import LiveMonitor, MonitorEvent
from trading_live_claude.signals.candle_exit import CandleExit
from trading_live_claude.signals.generator import SignalSet
from trading_live_claude.signals.overbought_exit import OverboughtExit
from trading_live_claude.strategies.base import Strategy, StrategyContext

OX = OverboughtExit()


def test_invalid_configuration_is_rejected() -> None:
    with pytest.raises(ValueError):
        OverboughtExit(rsi_level=40.0)
    with pytest.raises(ValueError):
        OverboughtExit(bb_window=1)
    with pytest.raises(ValueError):
        OverboughtExit(cost_multiple=0.5)


def _frame(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="D")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c - 0.1, "high": c + 0.3, "low": c - 0.3, "close": c,
                         "entry": 0, "exit": 0, "atr": 1.0}, index=idx)


def _chop_then_run() -> list[float]:
    chop = [100.0 + (0.4 if i % 2 else -0.4) for i in range(30)]      # RSI ~50, tight bands
    run = [100.0 + 1.2 * k for k in range(1, 8)]                       # steady rally, no down days
    return chop + run + [108.0, 108.2, 108.1]


def test_fires_on_a_stretch_and_not_in_the_chop() -> None:
    df = _frame(_chop_then_run())
    f = OX.fired(df)
    assert not f.iloc[20:30].any(axis=None)                           # chop: neither leg
    assert f["close_above_upper_bb"].iloc[31]                         # first rally bars break the band
    assert f["rsi_overbought"].iloc[33]                                # no-loss run scores as RSI 100


def test_no_down_day_run_is_scored_overbought_not_unknown() -> None:
    closes = [100.0 + i for i in range(40)]                            # never a loss: rsi() is NaN
    f = OX.fired(_frame(closes))
    assert f["rsi_overbought"].iloc[20:].all()
    assert not f["rsi_overbought"].iloc[:10].any()                     # warm-up stays quiet


def test_overbought_winner_exits_on_the_next_bar_and_loser_does_not() -> None:
    df = _frame(_chop_then_run())
    df.iloc[29, df.columns.get_loc("entry")] = 1                       # enter bar 30 at 100.4
    pos = SignalSet(df).to_positions(overbought_exit=OX)
    fired = OX.any_fired(df).to_numpy()
    close = df["close"].to_numpy()
    # first bar k after entry where the signal fired and the gain was >= 1 ATR -> flat on k+1
    k = next(i for i in range(31, len(df)) if fired[i] and close[i] - close[30] >= 1.0)
    assert pos.iloc[k] == 1 and pos.iloc[k + 1] == 0
    assert SignalSet(df).to_positions().iloc[-1] == 1                  # held without the exit
    # entering at the top: the stretch is there but the position is not a winner
    df2 = df.copy()
    df2["entry"] = 0
    df2.iloc[len(df2) - 4, df2.columns.get_loc("entry")] = 1
    assert SignalSet(df2).to_positions(overbought_exit=OX).iloc[-1] == 1


def test_overbought_exit_has_no_lookahead() -> None:
    df = _frame(_chop_then_run())
    df.iloc[29, df.columns.get_loc("entry")] = 1
    full = SignalSet(df).to_positions(overbought_exit=OX, candle_exit=CandleExit())
    for n in range(32, len(df) + 1):
        part = SignalSet(df.iloc[:n].copy()).to_positions(overbought_exit=OX, candle_exit=CandleExit())
        pd.testing.assert_series_equal(part, full.iloc[:n], check_names=False)


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

    def __init__(self, entry: float) -> None:
        self.entry = entry

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        return 100_000.0

    def positions(self, account_number: str) -> list[_Pos]:
        return [_Pos("AAA", 100.0, self.entry, 108.1)]

    def quote(self, symbol: str) -> _Quote:
        return _Quote(mid=108.1, lastTradePrice=108.1, bidPrice=108.09, askPrice=108.11)


class _Market:
    def recent(self, symbol: str, bars: int, interval: str = "1d") -> pd.DataFrame:
        closes = _chop_then_run()
        return _frame(closes).drop(columns=["entry", "exit", "atr"]).reset_index(drop=True)


class _Router:
    def __init__(self) -> None:
        self.intents: list[object] = []

    def submit(self, intent, **kw) -> None:
        self.intents.append(intent)


class _Quiet(Strategy):
    name = "quiet"

    def required_history_bars(self) -> int:
        return 5

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        out["entry"] = 0
        out["exit"] = 0
        out["atr"] = 1.0
        return out


def _live(entry: float, exempt: set[str] | None = None) -> tuple[_Router, list[MonitorEvent]]:
    router, events = _Router(), []
    mon = LiveMonitor(broker=_Broker(entry), market=_Market(), strategy=_Quiet(),   # type: ignore[arg-type]
                      sizer=None, router=router, account_number="X", symbols=["AAA"],  # type: ignore[arg-type]
                      on_event=events.append, emit_on_change_only=False, risk_model="atr",
                      heat_aggregation="sum", overbought_exit=OX, overbought_exit_exempt=exempt)
    mon.step()
    return router, events


def test_live_loop_exits_a_winner_on_an_overbought_completed_bar() -> None:
    df = _Market().recent("AAA", 0)
    assert OX.fired(df).iloc[-2].any()                                 # the completed bar is stretched
    router, events = _live(entry=100.0)
    assert len(router.intents) == 1
    ev = events[-1]
    assert ev.kind == "exit" and ev.detail["reason"] == "overbought_exit"
    assert set(ev.detail["patterns"]) <= {"close_above_upper_bb", "rsi_overbought"}
    assert ev.detail["pattern_bar_close"] == pytest.approx(108.2)


def test_live_loop_leaves_losers_and_exempt_strategies_alone() -> None:
    router, events = _live(entry=110.0)
    assert not router.intents and events[-1].kind == "hold"
    router, events = _live(entry=100.0, exempt={"quiet"})
    assert not router.intents and events[-1].kind == "hold"


def test_fired_is_past_only() -> None:
    df = _frame(_chop_then_run())
    full = OX.fired(df)
    for n in (25, 31, 34, len(df)):
        part = OX.fired(df.iloc[:n])
        assert np.array_equal(part.to_numpy(), full.iloc[:n].to_numpy())
