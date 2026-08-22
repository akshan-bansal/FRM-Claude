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
