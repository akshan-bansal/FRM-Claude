"""Strategy and risk-gate versions (AUDIT_LEDGER_SCOPE.md phase 4).

A version is only useful if it moves when behaviour moves and holds still when it doesn't. These
tests are mostly that pair of properties, because a version that silently fails to change is worse
than no version at all: it asserts "same code, same settings" when that is false.
"""
from __future__ import annotations

from pathlib import Path

from tests.test_router import _intent, _StubBroker
from trading_live_claude.audit import Ledger
from trading_live_claude.audit.versioning import (
    UNKNOWN,
    gate_thresholds,
    strategy_params,
    strategy_version,
)
from trading_live_claude.execution.router import Router
from trading_live_claude.strategies import STRATEGIES

# --- strategy version ------------------------------------------------------------------------

def test_same_strategy_and_params_version_identically() -> None:
    a, b = STRATEGIES["bollinger"](), STRATEGIES["bollinger"]()
    assert strategy_version(a) == strategy_version(b)
    assert strategy_version(a) != UNKNOWN


def test_different_params_version_differently() -> None:
    """`bollinger(n_std=2.0)` and `bollinger(n_std=2.5)` are different strategies to a later reader."""
    base = STRATEGIES["bollinger"]()
    tuned = STRATEGIES["bollinger"](n_std=2.5)
    assert strategy_version(base) != strategy_version(tuned)


def test_different_strategies_version_differently() -> None:
    assert strategy_version(STRATEGIES["bollinger"]()) != strategy_version(
        STRATEGIES["rsi_meanrevert"]())


def test_an_overridden_exit_knob_changes_the_version() -> None:
    """The per-trade exit knobs change behaviour, so they are part of the version."""
    base = STRATEGIES["bollinger"]()
    trailing = STRATEGIES["bollinger"]()
    trailing.trail_atr_mult = 4.0
    assert strategy_version(base) != strategy_version(trailing)


def test_params_capture_both_init_kwargs_and_exit_knobs() -> None:
    params = strategy_params(STRATEGIES["bollinger"](window=30))
    assert params["window"] == 30
    assert "time_stop_bars" in params and "trail_atr_mult" in params


def test_an_unversionable_object_reports_unknown_not_a_hash() -> None:
    """"Unversioned" must never be indistinguishable from "unchanged"."""
    dyn = type("_Dyn", (), {"name": "synthetic"})
    dyn.__module__ = "__nonexistent_module__"       # inspect can find no source for this
    assert strategy_version(dyn()) == UNKNOWN


# --- risk check version ----------------------------------------------------------------------

def test_the_same_gate_configuration_versions_identically(tmp_path: Path) -> None:
    a = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path)
    b = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path)
    assert a.risk_check_version() == b.risk_check_version() != UNKNOWN


def test_every_threshold_moves_the_version(tmp_path: Path) -> None:
    """A gate change that left the version alone would make old rows silently misleading."""
    base = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path)
    baseline = base.risk_check_version()
    variants = {
        "min_ticket_usd": dict(min_ticket_usd=250.0),
        "max_open_positions": dict(max_open_positions=9),
        "cap_pct": dict(cap_pct=0.09),
        "max_drawdown_pct": dict(max_drawdown_pct=0.07),
        "daily_loss_limit_pct": dict(daily_loss_limit_pct=0.07),
        "max_gross_leverage": dict(max_gross_leverage=1.5),
        "max_position_notional_pct": dict(max_position_notional_pct=0.25),
        "force_exit_atr_mult": dict(force_exit_atr_mult=4.0),
        "on_size_cap_breach": dict(on_size_cap_breach="reject"),
    }
    for label, kwargs in variants.items():
        router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                      **kwargs)
        assert router.risk_check_version() != baseline, f"{label} did not change the version"


def test_the_version_is_cached_but_reflects_construction(tmp_path: Path) -> None:
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path)
    first = router.risk_check_version()
    assert router.risk_check_version() is first          # cached, not recomputed per event


def test_thresholds_are_read_from_the_live_router(tmp_path: Path) -> None:
    """Read off the router, not from settings: the version must describe what is enforced."""
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                  min_ticket_usd=777.0, max_open_positions=4)
    t = gate_thresholds(router)
    assert t["min_ticket_usd"] == 777.0 and t["max_open_positions"] == 4
    assert t["mode"] == "paper" and t["heat_cap_pct"] is not None


# --- both, in the ledger ---------------------------------------------------------------------

def test_ledger_rows_carry_both_versions(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "ledger", stream="v", session_id="s1")
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                  ledger=led)
    strat = STRATEGIES["bollinger"]()
    router.strategy_version_for = lambda name: strategy_version(strat) if name == "bollinger" else ""
    intent = _intent()
    intent.strategy = "bollinger"
    router.submit(intent, equity=100_000.0, existing_risk=0.0, open_positions=0)

    rows = led.rows()
    assert rows, "no ledger rows written"
    for row in rows:
        assert row["risk_check_version"] == router.risk_check_version()
        assert row["strategy_version"] == strategy_version(strat)
        assert row["strategy_id"] == "bollinger"
    assert led.verify() == (True, "ok")


def test_an_intents_own_version_wins_over_the_resolver(tmp_path: Path) -> None:
    """A replayed or reconstructed intent keeps the version it was created with."""
    led = Ledger(tmp_path / "ledger", stream="v", session_id="s1")
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                  ledger=led)
    router.strategy_version_for = lambda name: "resolver-value"
    intent = _intent()
    intent.strategy_version = "pinned-value"
    router.submit(intent, equity=100_000.0, existing_risk=0.0, open_positions=0)
    assert {r["strategy_version"] for r in led.rows()} == {"pinned-value"}


def test_an_unattributable_intent_records_no_strategy_version(tmp_path: Path) -> None:
    """A flatten or hedge intent is not strategy-driven; guessing a version would be a lie."""
    led = Ledger(tmp_path / "ledger", stream="v", session_id="s1")
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                  ledger=led)
    router.strategy_version_for = lambda name: ""          # unknown name -> empty
    intent = _intent()
    intent.strategy = "flatten"
    router.submit(intent, equity=100_000.0, existing_risk=0.0, open_positions=0)
    assert {r["strategy_version"] for r in led.rows()} == {None}


def test_a_broken_resolver_cannot_break_a_trade(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "ledger", stream="v", session_id="s1")
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                  ledger=led)

    def _boom(_name: str) -> str:
        raise RuntimeError("resolver exploded")

    router.strategy_version_for = _boom
    order = router.submit(_intent(), equity=100_000.0, existing_risk=0.0, open_positions=0)
    assert order is not None                               # the trade still happened
    assert {r["strategy_version"] for r in led.rows()} == {None}


def test_the_monitor_installs_a_resolver_reflecting_its_own_instances(tmp_path: Path) -> None:
    """The monitor holds the tuned instances; the router only ever sees a name."""
    from tests.test_monitor import _Broker, _Market
    from trading_live_claude.monitor.live_loop import LiveMonitor

    tuned = STRATEGIES["bollinger"](n_std=2.75)          # a parameter set no default would match
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path)
    LiveMonitor(
        broker=_Broker(),          # type: ignore[arg-type]
        market=_Market(),          # type: ignore[arg-type]
        strategy=tuned,
        sizer=None,                # type: ignore[arg-type]
        router=router,
        account_number="X",
        symbols=["AAA"],
        on_event=lambda _e: None,
    )
    assert callable(router.strategy_version_for)
    assert router.strategy_version_for("bollinger") == strategy_version(tuned)
    assert router.strategy_version_for("bollinger") != strategy_version(STRATEGIES["bollinger"]())
    assert router.strategy_version_for("not-a-strategy") == ""


def test_a_caller_supplied_resolver_is_not_overwritten(tmp_path: Path) -> None:
    """An explicit resolver wins: the monitor must not clobber a deliberate wiring."""
    from tests.test_monitor import _Broker, _Market
    from trading_live_claude.monitor.live_loop import LiveMonitor

    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path)
    router.strategy_version_for = lambda _name: "explicit"
    LiveMonitor(
        broker=_Broker(),          # type: ignore[arg-type]
        market=_Market(),          # type: ignore[arg-type]
        strategy=STRATEGIES["bollinger"](),
        sizer=None,                # type: ignore[arg-type]
        router=router,
        account_number="X",
        symbols=["AAA"],
        on_event=lambda _e: None,
    )
    assert router.strategy_version_for("bollinger") == "explicit"
