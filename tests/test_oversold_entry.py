"""Variant #4: oversold entry on trend pullbacks (signals.oversold_entry) and its live-loop wiring."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.monitor.live_loop import LiveMonitor, MonitorEvent
from trading_live_claude.risk.sizing import PositionSizer
from trading_live_claude.signals.oversold_entry import OversoldEntry
from trading_live_claude.strategies.base import Strategy, StrategyContext

OE = OversoldEntry()


def test_invalid_configuration_is_rejected() -> None:
    with pytest.raises(ValueError):
        OversoldEntry(rsi_level=70.0)
    with pytest.raises(ValueError):
        OversoldEntry(bb_window=1)
    with pytest.raises(ValueError):
        OversoldEntry(cooldown_bars=0)


def _chop_then_drop() -> list[float]:
    chop = [100.0 + (0.4 if i % 2 else -0.4) for i in range(30)]      # RSI ~50, tight bands
    return chop + [100.0 - 1.2 * k for k in range(1, 11)]              # steady sell-off to the end


def _frame(closes: list[float]) -> pd.DataFrame:
    t = pd.date_range("2024-01-01", periods=len(closes), freq="D", tz="UTC")
    c = pd.Series(closes, dtype=float)
    return pd.DataFrame({"time": t, "open": c + 0.1, "high": c + 0.3, "low": c - 0.3, "close": c})


def test_fires_on_a_dip_and_not_in_the_chop() -> None:
    f = OE.fired(_frame(_chop_then_drop()))
    assert not f.iloc[20:30].any(axis=None)
    assert f["close_below_lower_bb"].iloc[31]
    assert f["rsi_oversold"].iloc[36] and f.iloc[-2].all()


def test_a_no_down_day_run_never_fires() -> None:
    f = OE.fired(_frame([100.0 + k for k in range(40)]))
    assert not f.any(axis=None)


def test_fired_is_past_only() -> None:
    df = _frame(_chop_then_drop())
    full = OE.fired(df)
    for n in (25, 31, 34, len(df)):
        assert np.array_equal(OE.fired(df.iloc[:n]).to_numpy(), full.iloc[:n].to_numpy())


def test_trend_filter_reads_the_strategy_signal_on_the_completed_bar() -> None:
    sig = pd.DataFrame({"entry": [1, 1, 0], "exit": [0, 0, 0]})
    assert OversoldEntry.trend_on(sig, -2)
    sig.loc[1, "exit"] = 1
    assert not OversoldEntry.trend_on(sig, -2)
    assert not OversoldEntry.trend_on(pd.DataFrame(), -2)


# ---- live loop ---------------------------------------------------------------------------------

@dataclass
class _Quote:
    mid: float
    lastTradePrice: float


@dataclass
class _Pos:
    symbol: str
    openQuantity: float
    averageEntryPrice: float
    currentPrice: float


class _Broker:
    name = "fake"

    def __init__(self, px: float = 88.5, held: float = 100.0) -> None:
        self.px, self.held = px, held

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        return 100_000.0

    def positions(self, account_number: str) -> list[_Pos]:
        return [_Pos("AAA", self.held, 90.0, self.px)] if self.held else []

    def quote(self, symbol: str) -> _Quote:
        return _Quote(mid=self.px, lastTradePrice=self.px)


class _Market:
    def recent(self, symbol: str, bars: int, interval: str = "1d") -> pd.DataFrame:
        return _frame(_chop_then_drop())


class _Router:
    def __init__(self) -> None:
        self.intents: list[object] = []

    def submit(self, intent, **kw):
        self.intents.append(intent)
        return type("O", (), {"totalQuantity": intent.shares})()


class _Trend(Strategy):
    """Trend filter configurable: ``on`` = entry 1 / exit 0 on every bar."""

    name = "ts_momentum"

    def __init__(self, on: bool = True) -> None:
        super().__init__()
        self.on = on

    def required_history_bars(self) -> int:
        return 5

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        out["entry"], out["exit"], out["atr"] = int(self.on), 0, 1.0
        return out


def _mon(router: _Router, events: list[MonitorEvent], *, strat: Strategy | None = None,
         broker: _Broker | None = None, **kw) -> LiveMonitor:
    return LiveMonitor(broker=broker or _Broker(), market=_Market(), strategy=strat or _Trend(),  # type: ignore[arg-type]
                       sizer=PositionSizer(risk_pct=0.01), router=router,                       # type: ignore[arg-type]
                       account_number="X", symbols=["AAA"], on_event=events.append,
                       emit_on_change_only=False, risk_model="atr", heat_aggregation="sum",
                       oversold_entry=OE, **kw)


def test_v4_adds_to_a_held_trend_position_on_an_oversold_completed_bar() -> None:
    router, events = _Router(), []
    _mon(router, events).step()
    assert len(router.intents) == 1 and router.intents[0].action.value == "Buy"  # type: ignore[attr-defined]
    ev = events[-1]
    assert ev.kind == "entry" and ev.detail["reason"] == "oversold_entry"
    assert set(ev.detail["patterns"]) <= {"close_below_lower_bb", "rsi_oversold"}  # type: ignore[arg-type]


def test_v4_never_buys_while_the_trend_is_off() -> None:
    router, events = _Router(), []
    _mon(router, events, strat=_Trend(on=False)).step()
    assert not router.intents and events[-1].kind == "hold"


def test_v4_only_applies_to_the_named_strategies() -> None:
    router, events = _Router(), []
    _mon(router, events, oversold_entry_only={"other"}).step()
    assert not router.intents


def test_v4_does_not_open_a_position_on_its_own() -> None:
    router, events = _Router(), []
    _mon(router, events, broker=_Broker(held=0.0)).step()
    # Not held: the strategy's own entry fires (trend on), never a V4-tagged add.
    assert all(e.detail.get("reason") != "oversold_entry" for e in events)


def test_v4_cooldown_blocks_a_second_add_on_the_same_bar() -> None:
    router, events = _Router(), []
    mon = _mon(router, events)
    mon.step()
    mon.step()
    assert len([i for i in router.intents if i.action.value == "Buy"]) == 1   # type: ignore[attr-defined]


def test_v4_tranche_stop_sells_only_the_tranche() -> None:
    router, events = _Router(), []
    broker = _Broker()
    mon = _mon(router, events, broker=broker)
    mon.step()
    tranche, stop = mon._v4_tranche["AAA"]
    broker.held += tranche                                  # the fake book reflects the fill
    broker.px = stop - 0.5
    mon.step()
    sell = router.intents[-1]
    assert sell.action.value == "Sell" and sell.shares == pytest.approx(tranche)  # type: ignore[attr-defined]
    assert events[-1].detail["reason"] == "oversold_entry_stop"
    assert "AAA" not in mon._v4_tranche
