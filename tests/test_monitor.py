from __future__ import annotations

import contextlib
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
    assert "halt_reason" in entries[0].detail
    assert entries[0].detail["mitigation"]["halt"] is True         # type: ignore[index]


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


def test_strategy_risk_gate_trims_size_without_osint() -> None:
    """The shipped vol-based strategy-risk gate scales size on its own (no OSINT configured)."""
    plain, gated = _Router(), _Router()
    _entry_monitor(plain, [], None).step()
    LiveMonitor(
        broker=_Broker(),          # type: ignore[arg-type]
        market=_Market(),          # type: ignore[arg-type]
        strategy=_EntryStrategy(),
        sizer=__import__("trading_live_claude.risk.sizing", fromlist=["PositionSizer"]).PositionSizer(risk_pct=0.01),
        router=gated,              # type: ignore[arg-type]
        account_number="X",
        symbols=["CCC"],
        strategy_risk=True,
    ).step()
    assert plain.intents and gated.intents
    # flat synthetic prices -> zero vol -> scalar stays 1.0, so sizing must be unchanged
    assert gated.intents[0].shares == plain.intents[0].shares


# --- persistence mode keeps edge information ------------------------------------------

def test_level_mode_marks_transitions_and_counts_polls() -> None:
    """Persistence emits every poll, but still says which emission was the transition."""
    strat = _ToggleStrategy()
    events: list[MonitorEvent] = []
    mon = _monitor(strat, events, edge=False)

    mon.step()   # first exit -> the transition
    mon.step()   # still exit -> persisting
    mon.step()   # still exit -> persisting
    assert [e.kind for e in events] == ["exit", "exit", "exit"]
    assert [e.is_transition for e in events] == [True, False, False]
    assert [e.poll_count for e in events] == [1, 2, 3]

    strat.exit_flag = 0          # flips to hold -> a fresh transition, run restarts
    mon.step()
    assert events[-1].is_transition is True
    assert events[-1].poll_count == 1


# --- interpret bias as entry filter --------------------------------------------------------

from trading_live_claude.intel.interpret import Thesis                       # noqa: E402


def test_interpret_bias_no_op_when_interpret_for_is_none() -> None:
    strat = _ToggleStrategy()
    events: list[MonitorEvent] = []
    mon = _monitor(strat, events, edge=True)
    assert mon.interpret_for is None
    bias, applied = mon._interpret_bias("EQB.TO")
    assert bias == 1.0 and applied == []


def test_interpret_bias_trims_when_symbol_in_moderate_thesis_exemplars() -> None:
    """A moderate thesis whose theme's exemplars include the symbol trims conviction to 0.75."""
    # XLE is in THEME_EXEMPLARS['energy']; use a moderate energy thesis.
    thesis = Thesis(name="Energy event concentration", confidence="moderate",
                    evidence=["x"], inference="…", action="…", themes=["energy"])
    strat = _ToggleStrategy()
    events: list[MonitorEvent] = []
    mon = _monitor(strat, events, edge=True)
    mon.interpret_for = lambda: [thesis]
    bias, applied = mon._interpret_bias("XLE")
    assert abs(bias - 0.75) < 1e-9
    assert applied == ["Energy event concentration"]


def test_interpret_bias_multiplies_multiple_theses_and_floors_at_25pct() -> None:
    """Stacked theses multiply; product cannot go below 0.25 (the interpret advisory floor)."""
    high1 = Thesis(name="Complacency divergence", confidence="high",
                    evidence=[], inference="", action="", themes=["safe_haven"])
    high2 = Thesis(name="Conflict escalation watch", confidence="high",
                    evidence=[], inference="", action="", themes=["safe_haven"])
    high3 = Thesis(name="Disaster / insurance underpricing", confidence="high",
                    evidence=[], inference="", action="", themes=["safe_haven"])
    # GLD is in safe_haven exemplars. Three high theses would give 0.5 * 0.5 * 0.5 = 0.125,
    # but the floor pins to 0.25.
    strat = _ToggleStrategy()
    events: list[MonitorEvent] = []
    mon = _monitor(strat, events, edge=True)
    mon.interpret_for = lambda: [high1, high2, high3]
    bias, applied = mon._interpret_bias("GLD")
    assert bias == 0.25
    assert len(applied) == 3


def test_interpret_bias_skips_the_null_thesis() -> None:
    """The quiet-tape null must never touch conviction — it is the honest 'no evidence' state."""
    null = Thesis(name="No notable configuration", confidence="high",
                    evidence=[], inference="", action="", themes=[])
    strat = _ToggleStrategy()
    events: list[MonitorEvent] = []
    mon = _monitor(strat, events, edge=True)
    mon.interpret_for = lambda: [null]
    bias, applied = mon._interpret_bias("XLE")
    assert bias == 1.0 and applied == []


def test_interpret_bias_tentative_theses_are_advisory_only() -> None:
    """A tentative-confidence thesis is documented as advisory — no size change."""
    tentative = Thesis(name="Sentiment stretch — greed", confidence="tentative",
                       evidence=[], inference="", action="", themes=["volatility_convexity"])
    strat = _ToggleStrategy()
    events: list[MonitorEvent] = []
    mon = _monitor(strat, events, edge=True)
    mon.interpret_for = lambda: [tentative]
    # VIXY is in volatility_convexity exemplars, but tentative confidence yields no trim.
    bias, applied = mon._interpret_bias("VIXY")
    assert bias == 1.0
    assert applied == []


def test_interpret_bias_never_raises_on_a_broken_interpret_for() -> None:
    """A broken interpret_for callable must not crash the poll — returns (1.0, [])."""
    def _broken() -> list[Thesis]:
        raise RuntimeError("interpret exploded")

    strat = _ToggleStrategy()
    events: list[MonitorEvent] = []
    mon = _monitor(strat, events, edge=True)
    mon.interpret_for = _broken
    bias, applied = mon._interpret_bias("XLE")
    assert bias == 1.0 and applied == []


def test_edge_mode_still_only_emits_transitions() -> None:
    """The hybrid must not change edge behaviour: still one alert per state change."""
    strat = _ToggleStrategy()
    events: list[MonitorEvent] = []
    mon = _monitor(strat, events, edge=True)

    mon.step()
    mon.step()
    mon.step()
    assert [e.kind for e in events] == ["exit"]          # repeats suppressed as before
    assert events[0].is_transition is True

    strat.exit_flag = 0
    mon.step()
    assert [e.kind for e in events] == ["exit", "hold"]
    assert all(e.is_transition for e in events)          # every edge emission is a transition


# ---------------------------------------------------------------------------
# flatten-on-exit — 2026-09-16
#
# Before this landed, terminating a session killed the process with positions still
# open: the final paper_equity.csv row carried non-zero positions_value and unrealized
# P&L that never resolved, and realized_pnl stayed 0.0 forever. Several sessions in the
# journal history have that shape. These tests pin the close-then-stop contract.
#
# Every exit routes through Router.submit — the risk gate is never bypassed on the way
# out — which means a flatten CAN be rejected (min-ticket on a residual, tripped
# kill-switch). A rejection must surface, never be swallowed into a half-flat book.
# ---------------------------------------------------------------------------


class _MutableBroker:
    """Broker whose position book can be emptied, so flatten's effect is observable."""

    name = "fake-mutable"

    def __init__(self, positions: list[_Position] | None = None) -> None:
        self._positions = list(positions if positions is not None else [_Position("AAA", 5)])
        self.mtm_calls = 0

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        return 100_000.0

    def positions(self, account_number: str) -> list[_Position]:
        return [p for p in self._positions if p.openQuantity != 0]

    def quote(self, symbol: str) -> _Quote:
        return _Quote(mid=10.0, lastTradePrice=10.0)

    def mark_to_market(self) -> None:
        self.mtm_calls += 1

    def close(self, symbol: str) -> None:
        self._positions = [p for p in self._positions if p.symbol != symbol]


class _FillingRouter:
    """Router that accepts every intent and closes the position on the broker, the way a
    real accepted SELL would once PaperBroker fills it."""

    def __init__(self, broker: _MutableBroker) -> None:
        self.broker = broker
        self.intents: list[object] = []

    def submit(self, intent, **kw):
        self.intents.append(intent)
        self.broker.close(intent.symbol)
        return object()          # non-None == accepted, mirrors Router.submit -> Order


class _RejectingRouter:
    """Router whose gate refuses everything — Router.submit returns None on rejection."""

    def __init__(self) -> None:
        self.intents: list[object] = []

    def submit(self, intent, **kw):
        self.intents.append(intent)
        return None


class _AlwaysHold(Strategy):
    """Never signals. Keeps step() from closing the book, so flatten is what closes it —
    with _AlwaysExit the strategy exit fires first and flatten finds an already-flat book."""

    name = "always_hold"

    def required_history_bars(self) -> int:
        return 3

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        out["entry"] = 0
        out["exit"] = 0
        out["atr"] = 1.0
        return out


def _flatten_monitor(broker, router, **kw) -> LiveMonitor:
    from trading_live_claude.risk.sizing import PositionSizer
    base = dict(
        broker=broker, market=_Market(), strategy=_AlwaysHold(),
        sizer=PositionSizer(risk_pct=0.01), router=router,
        account_number="ACC", symbols=["AAA"], interval_seconds=5,
        risk_model="atr", heat_aggregation="sum",
    )
    base.update(kw)
    return LiveMonitor(**base)


def test_flatten_closes_every_position_through_the_router() -> None:
    broker = _MutableBroker([_Position("AAA", 5), _Position("BBB", 3)])
    router = _FillingRouter(broker)
    mon = _flatten_monitor(broker, router)

    failed = mon.flatten()

    assert failed == []
    assert broker.positions("ACC") == []            # book is flat
    assert {i.symbol for i in router.intents} == {"AAA", "BBB"}
    assert all(i.action.value == "Sell" for i in router.intents)


def test_flatten_sells_the_exact_held_quantity() -> None:
    """Truncating would strand a fractional crypto position with no way out."""
    broker = _MutableBroker([_Position("PAXG/USD", 0.36)])
    router = _FillingRouter(broker)
    mon = _flatten_monitor(broker, router, symbols=["PAXG/USD"])

    mon.flatten()

    assert len(router.intents) == 1
    assert router.intents[0].shares == 0.36


def test_flatten_reports_gate_rejections_instead_of_swallowing_them() -> None:
    """A rejected exit must be returned AND leave the position visibly open — a silently
    half-flattened book is the exact failure this method exists to prevent."""
    broker = _MutableBroker([_Position("AAA", 5), _Position("BBB", 3)])
    router = _RejectingRouter()
    mon = _flatten_monitor(broker, router)

    failed = mon.flatten()

    assert sorted(failed) == ["AAA", "BBB"]
    assert len(broker.positions("ACC")) == 2        # nothing closed
    assert len(router.intents) == 2                 # but both were attempted


def test_flatten_on_an_already_flat_book_is_a_no_op() -> None:
    broker = _MutableBroker([])
    router = _FillingRouter(broker)
    mon = _flatten_monitor(broker, router)

    assert mon.flatten() == []
    assert router.intents == []


def test_flatten_remarks_the_book_so_the_final_equity_row_is_post_close() -> None:
    """MTM runs before (fresh exit prices) and after (flat book in the journal)."""
    broker = _MutableBroker([_Position("AAA", 5)])
    mon = _flatten_monitor(broker, _FillingRouter(broker))

    mon.flatten()

    assert broker.mtm_calls >= 2


def test_run_forever_flattens_on_exit_when_enabled() -> None:
    broker = _MutableBroker([_Position("AAA", 5)])
    router = _FillingRouter(broker)
    mon = _flatten_monitor(broker, router, flatten_on_exit=True, interval_seconds=5)

    mon.run_forever(max_iterations=1)

    assert broker.positions("ACC") == []
    assert any(getattr(i, "strategy", "") == "flatten" for i in router.intents)


def test_run_forever_leaves_the_book_open_when_flatten_is_disabled() -> None:
    """The pre-2026-09-16 behaviour must still be reachable — and must be opt-out, not silent."""
    broker = _MutableBroker([_Position("AAA", 5)])
    router = _FillingRouter(broker)
    mon = _flatten_monitor(broker, router, flatten_on_exit=False, interval_seconds=5)

    mon.run_forever(max_iterations=1)

    assert not any(getattr(i, "strategy", "") == "flatten" for i in router.intents)


def test_request_stop_exits_the_loop_through_the_finally() -> None:
    """Cooperative stop must leave via the finally so the flatten still runs."""
    broker = _MutableBroker([_Position("AAA", 5)])
    router = _FillingRouter(broker)
    mon = _flatten_monitor(broker, router, flatten_on_exit=True, interval_seconds=5)
    mon.request_stop()

    mon.run_forever()          # no max_iterations — only the stop flag ends this

    assert broker.positions("ACC") == []
    assert any(getattr(i, "strategy", "") == "flatten" for i in router.intents)


def test_flatten_still_runs_when_the_loop_raises() -> None:
    """An exception propagating out of run_forever must not skip the close."""
    broker = _MutableBroker([_Position("AAA", 5)])
    router = _FillingRouter(broker)
    mon = _flatten_monitor(broker, router, flatten_on_exit=True, interval_seconds=5)

    def _boom() -> None:
        raise RuntimeError("sleep interrupted")
    mon._sleep_seconds = _boom          # type: ignore[method-assign]

    with contextlib.suppress(RuntimeError):
        mon.run_forever()

    assert broker.positions("ACC") == []


# --- graceful-stop sentinel ------------------------------------------------
# TaskStop / a process-manager terminate is a HARD kill on Windows and never reaches a
# signal handler — measured 2026-09-16: the flatten did NOT run. The sentinel is the
# path that actually works for background sessions, so these tests matter more than the
# signal-handler ones.


class _SessionBroker(_MutableBroker):
    """MutableBroker with a PaperBroker-style session_id, for per-session sentinels."""

    def __init__(self, session_id: str, positions=None) -> None:
        super().__init__(positions)
        self.session_id = session_id


def test_global_stop_sentinel_ends_the_loop_and_flattens(tmp_path) -> None:
    broker = _MutableBroker([_Position("AAA", 5)])
    router = _FillingRouter(broker)
    mon = _flatten_monitor(broker, router, flatten_on_exit=True,
                           stop_sentinel_dir=tmp_path)
    (tmp_path / "STOP").write_text("stop", encoding="utf-8")

    mon.run_forever()          # no max_iterations — only the sentinel can end this

    assert broker.positions("ACC") == []
    assert any(getattr(i, "strategy", "") == "flatten" for i in router.intents)


def test_stop_sentinel_is_consumed_so_the_next_launch_is_not_killed(tmp_path) -> None:
    """A persistent STOP would immediately kill every subsequent session — unlike HALTED,
    which is a risk state and stays put. The sentinel is a one-shot request."""
    broker = _MutableBroker([_Position("AAA", 5)])
    mon = _flatten_monitor(broker, _FillingRouter(broker), flatten_on_exit=True,
                           stop_sentinel_dir=tmp_path)
    sentinel = tmp_path / "STOP"
    sentinel.write_text("stop", encoding="utf-8")

    mon.run_forever()

    assert not sentinel.exists()


def test_per_session_sentinel_only_stops_that_session(tmp_path) -> None:
    mine = _SessionBroker("aaaa1111", [_Position("AAA", 5)])
    mon = _flatten_monitor(mine, _FillingRouter(mine), flatten_on_exit=True,
                           stop_sentinel_dir=tmp_path)
    (tmp_path / "STOP_aaaa1111").write_text("stop", encoding="utf-8")

    mon.run_forever()

    assert mine.positions("ACC") == []


def test_another_sessions_sentinel_is_ignored(tmp_path) -> None:
    """A sentinel naming a different session must not stop this one."""
    mine = _SessionBroker("aaaa1111", [_Position("AAA", 5)])
    mon = _flatten_monitor(mine, _FillingRouter(mine), flatten_on_exit=True,
                           stop_sentinel_dir=tmp_path)
    (tmp_path / "STOP_bbbb2222").write_text("stop", encoding="utf-8")

    mon.run_forever(max_iterations=1)   # bounded, else this would never end

    assert (tmp_path / "STOP_bbbb2222").exists()     # not consumed by the wrong session


def test_sentinel_present_before_the_first_poll_opens_no_book(tmp_path) -> None:
    """Dropped between launch and the first step, the sentinel must stop the session
    rather than letting it open positions first."""
    broker = _MutableBroker([])          # starts flat
    router = _FillingRouter(broker)
    mon = _flatten_monitor(broker, router, flatten_on_exit=True,
                           stop_sentinel_dir=tmp_path)
    (tmp_path / "STOP").write_text("stop", encoding="utf-8")

    mon.run_forever()

    assert router.intents == []          # step() never ran, so nothing was routed


def test_no_sentinel_dir_means_the_feature_is_inert(tmp_path) -> None:
    """Callers that don't opt in must be completely unaffected."""
    broker = _MutableBroker([_Position("AAA", 5)])
    mon = _flatten_monitor(broker, _FillingRouter(broker), flatten_on_exit=False)
    (tmp_path / "STOP").write_text("stop", encoding="utf-8")

    mon.run_forever(max_iterations=1)     # bounded: no sentinel watching, so this is the only exit

    assert (tmp_path / "STOP").exists()


# --- warm-up cadence -------------------------------------------------------
# Faster polling for the first N minutes after launch, then the base interval, in the same
# process. A restart to change cadence would flatten the book, so this has to be in-loop.


def _warm_monitor(monkeypatch, *, base: int, warm: int | None, minutes: float, elapsed_s: float):
    import trading_live_claude.monitor.live_loop as ll
    clock = {"t": 1000.0}
    monkeypatch.setattr(ll.time, "monotonic", lambda: clock["t"])
    broker = _MutableBroker([])
    mon = _flatten_monitor(broker, _FillingRouter(broker), interval_seconds=base,
                           warmup_interval_seconds=warm, warmup_minutes=minutes)
    clock["t"] += elapsed_s
    return mon


def test_warmup_polls_faster_inside_the_window(monkeypatch) -> None:
    mon = _warm_monitor(monkeypatch, base=300, warm=60, minutes=60, elapsed_s=30 * 60)
    assert mon._sleep_seconds() == 60.0


def test_warmup_falls_back_to_base_after_the_window(monkeypatch) -> None:
    mon = _warm_monitor(monkeypatch, base=300, warm=60, minutes=60, elapsed_s=60 * 60 + 1)
    assert mon._sleep_seconds() == 300.0


def test_warmup_disabled_by_default(monkeypatch) -> None:
    mon = _warm_monitor(monkeypatch, base=300, warm=None, minutes=60, elapsed_s=0)
    assert mon._sleep_seconds() == 300.0


def test_warmup_never_slows_the_base_interval(monkeypatch) -> None:
    """A warm-up value larger than the base must not lengthen the sleep."""
    mon = _warm_monitor(monkeypatch, base=60, warm=300, minutes=60, elapsed_s=0)
    assert mon._sleep_seconds() == 60.0


def test_warmup_interval_is_floored_at_five_seconds(monkeypatch) -> None:
    mon = _warm_monitor(monkeypatch, base=300, warm=1, minutes=60, elapsed_s=0)
    assert mon._sleep_seconds() == 5.0
