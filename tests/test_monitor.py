from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from trading_live_claude.monitor.live_loop import LiveMonitor, MonitorEvent
from trading_live_claude.strategies.base import Strategy, StrategyContext


@dataclass
class _Quote:
    mid: float
    lastTradePrice: float


@dataclass
class _Position:
    symbol: str
    openQuantity: float


class _Broker:
    name = "fake"

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        return 100_000.0

    def positions(self, account_number: str) -> list[_Position]:
        return [_Position("AAA", 5)]  # we hold AAA, so the exit path is live

    def quote(self, symbol: str) -> _Quote:
        return _Quote(mid=10.0, lastTradePrice=10.0)


class _Market:
    def recent(self, symbol: str, bars: int, interval: str = "1d") -> pd.DataFrame:
        n = bars + 2
        return pd.DataFrame({"close": [10.0] * n, "high": [10.1] * n, "low": [9.9] * n})


class _Router:
    def __init__(self) -> None:
        self.intents: list[object] = []

    def submit(self, intent, **kw) -> None:
        self.intents.append(intent)
        return None


class _AlwaysExit(Strategy):
    """Always fires an exit; instance name is configurable to trace routing."""

    name = "always_exit"

    def __init__(self, label: str) -> None:
        super().__init__()
        self.name = label

    def required_history_bars(self) -> int:
        return 3

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        out["entry"] = 0
        out["exit"] = 1
        out["atr"] = 1.0
        return out


class _ToggleStrategy(Strategy):
    """Strategy whose last-bar exit signal we flip between polls."""

    name = "toggle"

    def __init__(self) -> None:
        super().__init__()
        self.exit_flag = 1

    def required_history_bars(self) -> int:
        return 3

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        out["entry"] = 0
        out["exit"] = self.exit_flag
        out["atr"] = 1.0
        return out


def _monitor(strat: _ToggleStrategy, collected: list[MonitorEvent], *, edge: bool) -> LiveMonitor:
    return LiveMonitor(
        broker=_Broker(),          # type: ignore[arg-type]
        market=_Market(),          # type: ignore[arg-type]
        strategy=strat,
        sizer=None,                # type: ignore[arg-type] - unused on the exit path
        router=_Router(),          # type: ignore[arg-type]
        account_number="X",
        symbols=["AAA"],
        on_event=collected.append,
        emit_on_change_only=edge,
    )


def test_edge_triggering_suppresses_repeat_alerts() -> None:
    strat = _ToggleStrategy()
    events: list[MonitorEvent] = []
    mon = _monitor(strat, events, edge=True)

    mon.step()  # exit -> alert (first time)
    mon.step()  # exit still true -> suppressed
    assert [e.kind for e in events] == ["exit"]

    strat.exit_flag = 0  # signal leaves exit -> transition to hold
    mon.step()
    assert [e.kind for e in events] == ["exit", "hold"]

    strat.exit_flag = 1  # back to exit -> transition fires again
    mon.step()
    assert [e.kind for e in events] == ["exit", "hold", "exit"]


def test_level_triggering_still_available() -> None:
    strat = _ToggleStrategy()
    events: list[MonitorEvent] = []
    mon = _monitor(strat, events, edge=False)

    mon.step()
    mon.step()
    assert [e.kind for e in events] == ["exit", "exit"]  # repeats, no dedupe


def test_strategy_for_resolves_per_symbol() -> None:
    default = _ToggleStrategy()
    override = _AlwaysExit("override")
    mon = LiveMonitor(
        broker=_Broker(),          # type: ignore[arg-type]
        market=_Market(),          # type: ignore[arg-type]
        strategy=default,
        sizer=None,                # type: ignore[arg-type]
        router=_Router(),          # type: ignore[arg-type]
        account_number="X",
        symbols=["AAA", "BBB"],
        strategy_map={"BBB": override},
    )
    assert mon._strategy_for("AAA") is default   # fallback
    assert mon._strategy_for("BBB") is override   # per-symbol override


def test_per_symbol_strategy_names_the_routed_order() -> None:
    router = _Router()
    special = _AlwaysExit("special_strat")
    mon = LiveMonitor(
        broker=_Broker(),          # type: ignore[arg-type]
        market=_Market(),          # type: ignore[arg-type]
        strategy=_ToggleStrategy(),
        sizer=None,                # type: ignore[arg-type]
        router=router,             # type: ignore[arg-type]
        account_number="X",
        symbols=["AAA"],           # AAA is held by _Broker → exit path fires
        strategy_map={"AAA": special},
    )
    mon.step()
    assert any(getattr(i, "strategy", None) == "special_strat" for i in router.intents)


def _hedge_monitor(events: list[MonitorEvent]) -> LiveMonitor:
    return LiveMonitor(
        broker=_Broker(),          # type: ignore[arg-type]
        market=_Market(),          # type: ignore[arg-type]
        strategy=_ToggleStrategy(),
        sizer=None,                # type: ignore[arg-type]
        router=_Router(),          # type: ignore[arg-type]
        account_number="X",
        symbols=["AAA"],
        on_event=events.append,
        hedge_symbol="UUP",
    )


def test_hedge_overlay_emits_rebalance_on_drawdown() -> None:
    events: list[MonitorEvent] = []
    mon = _hedge_monitor(events)
    mon._equity_peak = 120_000.0  # prior high vs the fixture's 100k equity → ~17% drawdown
    mon.step()
    hedges = [e for e in events if e.kind == "hedge"]
    assert len(hedges) == 1
    h = hedges[0]
    assert h.symbol == "UUP"
    assert h.detail["delta"] > 0        # buy the dollar sleeve as the book draws down
    assert h.detail["weight"] > 0.0
    assert h.detail["drawdown"] < -0.1


def test_hedge_overlay_quiet_at_the_highs() -> None:
    events: list[MonitorEvent] = []
    mon = _hedge_monitor(events)
    mon.step()  # peak == equity → 0 drawdown → weight 0 → no hedge rebalance
    assert not [e for e in events if e.kind == "hedge"]


# --- WorldMonitor risk overlay gating -------------------------------------------------

class _EntryStrategy(Strategy):
    """Always fires an entry on the last bar (for the non-held entry path)."""

    name = "always_entry"

    def required_history_bars(self) -> int:
        return 3

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        out["entry"] = 1
        out["exit"] = 0
        out["atr"] = 1.0
        return out


def _decision(scalar: float, halt: bool):
    from trading_live_claude.intel.overlay import OverlayDecision
    return OverlayDecision(asset_class="equity", scalar=scalar, halt_new_entries=halt,
                           reasons=[], components={})


def _entry_monitor(router, events, overlay_for):
    from trading_live_claude.risk.sizing import PositionSizer
    return LiveMonitor(
        broker=_Broker(),          # type: ignore[arg-type]
        market=_Market(),          # type: ignore[arg-type]
        strategy=_EntryStrategy(),
        sizer=PositionSizer(risk_pct=0.01),
        router=router,             # type: ignore[arg-type]
        account_number="X",
        symbols=["CCC"],           # not held by _Broker → entry path fires
        on_event=events.append,
        overlay_for=overlay_for,
    )


def test_overlay_halt_blocks_new_entry_but_still_alerts() -> None:
    router = _Router()
    events: list[MonitorEvent] = []
    _entry_monitor(router, events, lambda _s: _decision(0.25, True)).step()
    assert router.intents == []                                   # routing blocked
    entries = [e for e in events if e.kind == "entry"]
    assert len(entries) == 1                                      # signal still surfaced
    assert "overlay_halt" in entries[0].detail


def test_overlay_trims_size_when_not_halted() -> None:
    full_router, trim_router = _Router(), _Router()
    _entry_monitor(full_router, [], lambda _s: _decision(1.0, False)).step()
    _entry_monitor(trim_router, [], lambda _s: _decision(0.5, False)).step()
    assert full_router.intents and trim_router.intents             # both route
    assert trim_router.intents[0].shares <= full_router.intents[0].shares


def test_overlay_never_blocks_exits() -> None:
    router = _Router()
    events: list[MonitorEvent] = []
    LiveMonitor(
        broker=_Broker(),          # type: ignore[arg-type]
        market=_Market(),          # type: ignore[arg-type]
        strategy=_AlwaysExit("x"),
        sizer=None,                # type: ignore[arg-type]
        router=router,             # type: ignore[arg-type]
        account_number="X",
        symbols=["AAA"],           # held → exit path
        on_event=events.append,
        overlay_for=lambda _s: _decision(0.25, True),  # halting overlay must NOT stop the exit
    ).step()
    assert any(getattr(i, "action", None) is not None for i in router.intents)  # exit routed
