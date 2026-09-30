"""Parallel entry allocation (risk.entry_allocation) and its live-loop wiring."""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd
import pytest

from trading_live_claude.monitor.live_loop import LiveMonitor, MonitorEvent
from trading_live_claude.risk.entry_allocation import EntryCandidate, allocate_entries
from trading_live_claude.risk.sizing import PositionSizer
from trading_live_claude.strategies.base import Strategy, StrategyContext


def _c(sym: str, shares: float, *, price: float = 10.0, conv: float = 1.0, cap: float = math.inf,
       new: bool = True) -> EntryCandidate:
    return EntryCandidate(sym, shares, price, conv, cap_notional=cap, new_position=new)


def test_budget_is_shared_pro_rata_not_first_come() -> None:
    # Three names each want $60k against $90k of headroom: each gets half, whatever the list order.
    out = {a.symbol: a for a in allocate_entries(
        [_c("AAA", 6000), _c("BBB", 6000), _c("CCC", 6000)], slots=None, budget=90_000)}
    assert [out[s].shares for s in ("AAA", "BBB", "CCC")] == [3000, 3000, 3000]
    assert all(a.scale == pytest.approx(0.5) for a in out.values())


def test_under_budget_keeps_the_sizer_proposal() -> None:
    out = allocate_entries([_c("AAA", 100), _c("BBB", 200)], slots=None, budget=1e9)
    assert {a.symbol: a.shares for a in out} == {"AAA": 100, "BBB": 200}


def test_slots_go_to_the_highest_conviction_names() -> None:
    out = {a.symbol: a for a in allocate_entries(
        [_c("AAA", 10, conv=0.2), _c("BBB", 10, conv=0.9), _c("CCC", 10, conv=0.5)],
        slots=2, budget=1e9)}
    assert out["BBB"].routed and out["CCC"].routed
    assert not out["AAA"].routed and "no position slot" in out["AAA"].dropped


def test_an_add_to_a_held_position_takes_no_slot() -> None:
    out = {a.symbol: a for a in allocate_entries(
        [_c("AAA", 10, new=False), _c("BBB", 10)], slots=0, budget=1e9)}
    assert out["AAA"].routed and not out["BBB"].routed


def test_per_symbol_cap_clips_before_the_budget_split() -> None:
    out = {a.symbol: a for a in allocate_entries(
        [_c("AAA", 10_000, cap=20_000), _c("BBB", 1000)], slots=None, budget=1e9)}
    assert out["AAA"].shares == 2000 and out["BBB"].shares == 1000


def test_rounding_to_zero_is_dropped_with_a_reason() -> None:
    out = allocate_entries([_c("AAA", 10, price=500.0)], slots=None, budget=100.0)
    assert not out[0].routed and "rounds to 0" in out[0].dropped


def test_no_budget_left_routes_nothing() -> None:
    assert not any(a.routed for a in allocate_entries([_c("AAA", 10)], slots=None, budget=-5.0))


# ---- live loop ---------------------------------------------------------------------------------

@dataclass
class _Quote:
    mid: float
    lastTradePrice: float


class _Broker:
    name = "fake"

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        return 100_000.0

    def positions(self, account_number: str) -> list[object]:
        return []

    def quote(self, symbol: str) -> _Quote:
        return _Quote(mid=10.0, lastTradePrice=10.0)


class _Market:
    def recent(self, symbol: str, bars: int, interval: str = "1d") -> pd.DataFrame:
        n = bars + 2
        return pd.DataFrame({"close": [10.0] * n, "high": [10.1] * n, "low": [9.9] * n})


class _CappedRouter:
    """Exposes the Router's caps the allocator reads; accepts everything it is sent."""

    max_open_positions = 2
    max_gross_leverage = 1.0

    def __init__(self) -> None:
        self.calls: list[tuple[object, int]] = []

    def _position_cap_pct(self, symbol: str) -> float:
        return 0.5

    def submit(self, intent, *, open_positions: int, **kw):
        self.calls.append((intent, open_positions))
        return type("O", (), {"totalQuantity": intent.shares})()


class _Entry(Strategy):
    name = "always_entry"

    def required_history_bars(self) -> int:
        return 3

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        out["entry"], out["exit"], out["atr"] = 1, 0, 0.05
        return out


def _monitor(router, events, **kw) -> LiveMonitor:
    return LiveMonitor(broker=_Broker(), market=_Market(), strategy=_Entry(),  # type: ignore[arg-type]
                       sizer=PositionSizer(risk_pct=0.01), router=router,        # type: ignore[arg-type]
                       account_number="X", symbols=["AAA", "BBB", "CCC"], on_event=events.append,
                       risk_model="atr", heat_aggregation="sum", **kw)


def test_parallel_sizing_respects_slots_budget_and_counts_opens_within_the_poll() -> None:
    router, events = _CappedRouter(), []
    _monitor(router, events, parallel_sizing=True).step()
    assert len(router.calls) == 2                                      # 2 free slots, 3 signals
    assert [n for _, n in router.calls] == [0, 1]                      # count advances per open
    notional = sum(i.shares * i.entry for i, _ in router.calls)        # type: ignore[attr-defined]
    assert notional <= 100_000 + 1e-6                                  # within 1.0x leverage
    dropped = [e for e in events if e.kind == "entry" and "dropped" in e.detail["allocation"]]  # type: ignore[operator]
    assert len(dropped) == 1 and dropped[0].detail["sized"] == 0


def test_sequential_sizing_is_unchanged_when_off() -> None:
    router, events = _CappedRouter(), []
    _monitor(router, events).step()
    assert len(router.calls) == 3                                      # old path: every signal routed
    assert [n for _, n in router.calls] == [0, 0, 0]


def test_muted_symbols_route_but_do_not_alert() -> None:
    router, events = _CappedRouter(), []
    _monitor(router, events, mute_symbols={"bbb"}).step()
    assert len(router.calls) == 3
    assert {e.symbol for e in events} == {"AAA", "CCC"}
    assert isinstance(events[0], MonitorEvent)


# ---- trim_to_slots (pro rata) ------------------------------------------------------------------

@dataclass
class _Held:
    symbol: str
    openQuantity: float
    currentPrice: float
    averageEntryPrice: float = 10.0


class _HeldBroker(_Broker):
    """Fully invested $100k book at $10/share: AAA $60k, BBB $30k, CCC $10k."""

    def __init__(self, book: dict[str, float] | None = None) -> None:
        self.book = book if book is not None else {"AAA": 6000.0, "BBB": 3000.0, "CCC": 1000.0}

    def positions(self, account_number: str) -> list[object]:
        return [_Held(s, q, 10.0) for s, q in self.book.items() if q]


class _SlotRouter(_CappedRouter):
    max_open_positions = 5


def _trim_monitor(broker: _HeldBroker, router: _SlotRouter, events: list[MonitorEvent]) -> LiveMonitor:
    return LiveMonitor(broker=broker, market=_Market(), strategy=_Entry(),  # type: ignore[arg-type]
                       sizer=PositionSizer(risk_pct=0.01), router=router,  # type: ignore[arg-type]
                       account_number="X", symbols=[], on_event=events.append,
                       risk_model="atr", heat_aggregation="sum")


def test_trim_to_slots_scales_every_holding_by_the_same_factor() -> None:
    router, events = _SlotRouter(), []
    rows = _trim_monitor(_HeldBroker(), router, events).trim_to_slots()
    # 3 held under a cap of 5: gross $100k -> $60k, x0.6 on every name, weights 60/30/10 kept.
    assert {r["symbol"]: r["kept"] for r in rows} == {"AAA": 3600, "BBB": 1800, "CCC": 600}
    assert all(i.action.value == "Sell" for i, _ in router.calls)       # type: ignore[attr-defined]
    assert sum(i.shares for i, _ in router.calls) == 4000               # type: ignore[attr-defined]
    assert all(e.detail["reason"] == "slot_trim" for e in events)


def test_trim_to_slots_does_nothing_when_there_is_already_room() -> None:
    router = _SlotRouter()
    broker = _HeldBroker({"AAA": 2000.0, "BBB": 1000.0})               # $30k gross vs $40k target
    assert _trim_monitor(broker, router, []).trim_to_slots() == [] and router.calls == []


def test_trim_to_slots_does_nothing_at_or_over_the_cap() -> None:
    router = _SlotRouter()
    router.max_open_positions = 3
    assert _trim_monitor(_HeldBroker(), router, []).trim_to_slots() == [] and router.calls == []


# ---- close_symbols (drop a name from the sleeve) ------------------------------------------------

def test_close_symbols_sells_only_the_named_positions_in_full() -> None:
    router, events = _SlotRouter(), []
    broker = _HeldBroker()
    rows = _trim_monitor(broker, router, events).close_symbols(["bbb"])   # case-insensitive
    assert [r["symbol"] for r in rows] == ["BBB"]
    (intent, _), = router.calls
    assert intent.action.value == "Sell" and intent.shares == 3000        # type: ignore[attr-defined]
    assert events[-1].detail["reason"] == "drop_symbol"


def test_close_symbols_is_a_no_op_for_a_name_not_held() -> None:
    router = _SlotRouter()
    assert _trim_monitor(_HeldBroker(), router, []).close_symbols(["ZZZ"]) == []
    assert router.calls == []
