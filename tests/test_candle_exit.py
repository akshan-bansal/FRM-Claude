"""Tests for signals/candle_exit.py (exit Variant #2) and its hooks in to_positions and the live loop."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pytest

from trading_live_claude.monitor.live_loop import LiveMonitor, MonitorEvent
from trading_live_claude.signals.candle_exit import BEARISH_REVERSAL, CandleExit
from trading_live_claude.signals.candlesticks import CANDLESTICK_PATTERNS
from trading_live_claude.signals.generator import SignalSet
from trading_live_claude.strategies.base import Strategy, StrategyContext

CE = CandleExit()


def test_defaults_are_the_bearish_mirrors_and_all_exist() -> None:
    assert len(BEARISH_REVERSAL) == 8
    assert all(p in CANDLESTICK_PATTERNS for p in BEARISH_REVERSAL)


def test_invalid_configuration_is_rejected() -> None:
    with pytest.raises(ValueError):
        CandleExit(patterns=("not_a_pattern",))
    with pytest.raises(ValueError):
        CandleExit(cost_multiple=0.5)
    with pytest.raises(ValueError):
        CandleExit(min_gain_atr=-1.0)


def test_armed_needs_the_atr_gain_and_the_cost_multiple() -> None:
    assert not CE.armed(entry=100.0, ref_price=100.9, atr=1.0)            # < 1 ATR
    assert CE.armed(entry=100.0, ref_price=101.0, atr=1.0)                # exactly 1 ATR
    assert not CE.armed(entry=100.0, ref_price=99.0, atr=1.0)             # losing
    # 1 ATR = 0.3% but 2x a 0.2% round trip = 0.4% -> costs block it
    assert not CE.armed(entry=100.0, ref_price=100.3, atr=0.3, round_trip_cost_frac=0.002)
    assert CE.armed(entry=100.0, ref_price=100.5, atr=0.3, round_trip_cost_frac=0.002)
    assert not CE.armed(entry=0.0, ref_price=1.0, atr=1.0)


# ---- backtest path -----------------------------------------------------------------------------

def _ohlc(rows: list[tuple[float, float]]) -> pd.DataFrame:
    """rows of (open, close); high/low wrap them by 0.1."""
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="D")
    o = pd.Series([r[0] for r in rows], index=idx, dtype=float)
    c = pd.Series([r[1] for r in rows], index=idx, dtype=float)
    return pd.DataFrame({"open": o, "close": c, "high": pd.concat([o, c], axis=1).max(axis=1) + 0.1,
                         "low": pd.concat([o, c], axis=1).min(axis=1) - 0.1,
                         "entry": 0, "exit": 0, "atr": 1.0}, index=idx)


def _winner_then_engulfing() -> pd.DataFrame:
    rows = [(99.5, 100.0), (99.5, 100.0), (100.5, 101.0), (101.5, 102.0), (102.5, 103.0),
            (103.5, 104.0),               # bar 5: bullish
            (104.2, 103.4),               # bar 6: bearish engulfing of bar 5
            (103.4, 103.6), (103.6, 103.8)]
    df = _ohlc(rows)
    df.iloc[0, df.columns.get_loc("entry")] = 1   # enter on bar 1 at 100.0
    return df


def test_bearish_engulfing_on_a_winner_exits_on_the_next_bar() -> None:
    df = _winner_then_engulfing()
    assert bool(CANDLESTICK_PATTERNS["bearish_engulfing"](df).iloc[6])
    plain = SignalSet(df).to_positions()
    ce = SignalSet(df).to_positions(candle_exit=CE)
    assert (plain.iloc[1:] == 1).all()
    assert ce.iloc[6] == 1 and ce.iloc[7] == 0 and (ce.iloc[7:] == 0).all()


def test_same_pattern_under_a_losing_position_is_ignored() -> None:
    df = _winner_then_engulfing()
    df["entry"] = 0
    df.iloc[4, df.columns.get_loc("entry")] = 1   # enter on bar 5 at 104.0: bar 6 is a loss
    ce = SignalSet(df).to_positions(candle_exit=CE)
    assert ce.iloc[-1] == 1


def test_candle_exit_has_no_lookahead() -> None:
    df = _winner_then_engulfing()
    full = SignalSet(df).to_positions(candle_exit=CE)
    for n in range(3, len(df) + 1):
        part = SignalSet(df.iloc[:n].copy()).to_positions(candle_exit=CE)
        pd.testing.assert_series_equal(part, full.iloc[:n], check_names=False)


def test_level_entry_is_locked_out_after_a_candle_exit() -> None:
    df = _winner_then_engulfing()
    df["entry"] = 1                                # level-style entry
    ce = SignalSet(df).to_positions(candle_exit=CE)
    assert ce.iloc[7] == 0 and (ce.iloc[7:] == 0).all()


def test_frame_without_ohlc_leaves_the_exit_inert() -> None:
    df = _winner_then_engulfing().drop(columns=["open"])
    assert SignalSet(df).to_positions(candle_exit=CE).iloc[-1] == 1


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

    def __init__(self, entry: float = 100.0) -> None:
        self.entry = entry

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        return 100_000.0

    def positions(self, account_number: str) -> list[_Pos]:
        return [_Pos("AAA", 100.0, self.entry, 103.6)]

    def quote(self, symbol: str) -> _Quote:
        return _Quote(mid=103.6, lastTradePrice=103.6, bidPrice=103.59, askPrice=103.61)


class _Market:
    def recent(self, symbol: str, bars: int, interval: str = "1d") -> pd.DataFrame:
        flat = [(99.9, 100.0)] * (bars - 1)
        rows = [*flat, (103.5, 104.0), (104.2, 103.4), (103.4, 103.6)]   # engulfing at iloc[-2]
        df = _ohlc(rows).drop(columns=["entry", "exit", "atr"]).reset_index(drop=True)
        return df


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


def _live(entry: float = 100.0, exempt: set[str] | None = None) -> tuple[_Router, list[MonitorEvent]]:
    router, events = _Router(), []
    mon = LiveMonitor(broker=_Broker(entry), market=_Market(), strategy=_Quiet(),   # type: ignore[arg-type]
                      sizer=None, router=router, account_number="X", symbols=["AAA"],  # type: ignore[arg-type]
                      on_event=events.append, emit_on_change_only=False, risk_model="atr",
                      heat_aggregation="sum", candle_exit=CE, candle_exit_exempt=exempt)
    mon.step()
    return router, events


def test_live_loop_exits_on_a_completed_bearish_candle_with_the_reason() -> None:
    router, events = _live()
    assert len(router.intents) == 1
    ev = events[-1]
    assert ev.kind == "exit" and ev.detail["reason"] == "candle_exit"
    assert "bearish_engulfing" in ev.detail["patterns"]
    assert ev.detail["pattern_bar_close"] == pytest.approx(103.4)


def test_live_loop_ignores_the_candle_on_a_loser_and_when_exempt() -> None:
    router, events = _live(entry=104.0)            # 103.4 close is below entry: not armed
    assert not router.intents and events[-1].kind == "hold"
    router, events = _live(exempt={"quiet"})
    assert not router.intents and events[-1].kind == "hold"
