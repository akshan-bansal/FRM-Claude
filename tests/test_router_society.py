"""Router x simulated-society influence (HOMOGENEOUS_GRAPH_SCOPE.md).

The contract under test: ``Router.society_view`` is OFF unless set; when a view applies it can only
SHRINK an ENTRY, exactly once, before any size gate; exits are never touched; a broken resolver
means "no view", never a failed trade. Synthetic throughout: no real simulation result exists.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trading_live_claude.brokers.models import OrderAction, Quote
from trading_live_claude.execution.router import OrderIntent, Router
from trading_live_claude.sim import AdapterParams, RunBudget, SimResult, SimSeed, SocietyView
from trading_live_claude.sim.contract import Stance

EQUITY = 100_000.0


class _Broker:
    name = "stub"

    def __init__(self) -> None:
        self.placed: list[Any] = []

    def accounts(self) -> list:
        return []

    def positions(self, _: str) -> list:
        return []

    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, symbolId=1, bidPrice=99.5, askPrice=100.5, lastTradePrice=100.0)

    def quotes(self, symbols: list[str]) -> list[Quote]:
        return [self.quote(s) for s in symbols]

    def candles(self, *a: Any, **k: Any) -> list:  # pragma: no cover
        return []

    def equity(self, _: str) -> float:
        return EQUITY

    def place_order(self, order: Any) -> Any:
        self.placed.append(order)
        order.id = 1
        return order

    def cancel_order(self, *_: Any, **__: Any) -> None:
        pass


class _Ledger:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, object]]] = []

    def append(self, event: str, payload: dict[str, object], **_: Any) -> None:
        self.rows.append((event, payload))


def _router(tmp_path: Path, **kw: Any) -> tuple[Router, _Broker]:
    b = _Broker()
    return Router.build_default(mode="paper", broker=b, state_dir=tmp_path, **kw), b


def _intent(action: OrderAction = OrderAction.BUY, shares: float = 100, entry: float = 100.0,
            **kw: Any) -> OrderIntent:
    stop = entry * 0.96 if action == OrderAction.BUY else entry * 1.04
    return OrderIntent(symbol="BTC/USD", action=action, shares=shares, entry=entry, stop=stop,
                       target=None, strategy="t", risk_dollars=400.0, account_number="PAPER-001", **kw)


def _submit(r: Router, i: OrderIntent):
    return r.submit(i, equity=EQUITY, existing_risk=0.0, open_positions=0)


def _journal_rows(tmp_path: Path) -> list[dict[str, Any]]:
    f = tmp_path / "orders.jsonl"
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]


# ---- off by default / one-sided / entries only ------------------------------------------------

def test_no_view_set_means_no_change_and_no_new_journal_keys(tmp_path: Path) -> None:
    r, b = _router(tmp_path)
    assert r.society_view is None
    _submit(r, _intent())
    assert b.placed[0].totalQuantity == 100
    assert "society_influence" not in _journal_rows(tmp_path)[0]


def test_a_view_shrinks_an_entry_and_the_journal_says_so(tmp_path: Path) -> None:
    r, b = _router(tmp_path)
    r.society_view = lambda sym: (0.5, "run-1")
    i = _intent()
    _submit(r, i)
    assert b.placed[0].totalQuantity == 50 and i.shares == 50
    row = _journal_rows(tmp_path)[0]
    assert row["society_influence"] == 0.5 and row["society_run_id"] == "run-1" and row["shares"] == 50


def test_exits_are_never_scaled(tmp_path: Path) -> None:
    r, b = _router(tmp_path)
    r.society_view = lambda sym: (0.1, "run-1")
    _submit(r, _intent(action=OrderAction.SELL, shares=100))
    assert b.placed[0].totalQuantity == 100
    _submit(r, _intent(action=OrderAction.SELL, shares=100, society_influence=0.1))
    assert b.placed[1].totalQuantity == 100


@pytest.mark.parametrize("bad", [5.0, 1.0, 1.0000001, float("inf")])
def test_an_influence_above_one_can_never_enlarge_an_order(tmp_path: Path, bad: float) -> None:
    r, b = _router(tmp_path)
    r.society_view = lambda sym: (bad, "run-1")
    _submit(r, _intent())
    assert b.placed[0].totalQuantity == 100


@pytest.mark.parametrize("bad", [0.0, -0.5, float("nan"), True, "0.5", None])
def test_an_unusable_influence_means_no_view_not_a_failed_trade(tmp_path: Path, bad: Any) -> None:
    r, b = _router(tmp_path)
    r.society_view = lambda sym: (bad, "run-1")                          # type: ignore[return-value]
    _submit(r, _intent())
    assert b.placed and b.placed[0].totalQuantity == 100


def test_a_raising_resolver_is_no_view(tmp_path: Path) -> None:
    r, b = _router(tmp_path)

    def boom(_: str) -> tuple[float, str]:
        raise RuntimeError("view exploded")

    r.society_view = boom
    _submit(r, _intent())
    assert b.placed[0].totalQuantity == 100


def test_an_influence_carried_on_the_intent_wins_over_the_resolver(tmp_path: Path) -> None:
    r, b = _router(tmp_path)
    r.society_view = lambda sym: (0.9, "resolver")
    _submit(r, _intent(society_influence=0.4, society_run_id="explicit"))
    assert b.placed[0].totalQuantity == 40
    assert _journal_rows(tmp_path)[0]["society_run_id"] == "explicit"


# ---- idempotency: the card path gates, waits, then submits the same object ---------------------

def test_scaling_happens_once_across_a_gate_then_submit(tmp_path: Path) -> None:
    r, b = _router(tmp_path)
    r.society_view = lambda sym: (0.5, "run-1")
    i = _intent()
    first = r._gate(i, equity=EQUITY, existing_risk=0.0, open_positions=0)       # what ApprovalRouter does
    assert first.accepted and i.shares == 50
    _submit(r, i)                                                                  # ...then submits it
    assert i.shares == 50 and b.placed[0].totalQuantity == 50                      # not 25


# ---- the size gates judge the SCALED size -----------------------------------------------------

def test_scaled_to_zero_shares_is_rejected_with_the_reason(tmp_path: Path) -> None:
    r, b = _router(tmp_path)
    r.society_view = lambda sym: (0.3, "run-1")
    assert _submit(r, _intent(shares=2, entry=100.0)) is None            # floor(0.6) = 0
    assert not b.placed
    assert "trims 2 shares to 0" in _journal_rows(tmp_path)[0]["rejected_reasons"][0]


def test_min_ticket_judges_the_scaled_notional(tmp_path: Path) -> None:
    r, b = _router(tmp_path, min_ticket_usd=5_000.0)
    _submit(r, _intent(shares=100, entry=100.0))                          # $10k: fine unscaled
    assert len(b.placed) == 1
    r.society_view = lambda sym: (0.4, "run-1")
    assert _submit(r, _intent(shares=100, entry=100.0)) is None           # $4k scaled: below $5k
    assert len(b.placed) == 1


def test_cost_cap_sees_the_scaled_notional_and_never_sizes_back_up(tmp_path: Path) -> None:
    r, b = _router(tmp_path, max_round_trip_cost_ratio=0.005)
    _submit(r, _intent(shares=100, entry=100.0))                          # $10k: ~0.2% round trip
    assert len(b.placed) == 1
    r.society_view = lambda sym: (0.2, "run-1")
    assert _submit(r, _intent(shares=100, entry=100.0)) is None           # $2k: fixed fee now ~0.6%
    assert len(b.placed) == 1
    reasons = _journal_rows(tmp_path)[-1]["rejected_reasons"]
    assert any("round-trip cost" in x for x in reasons)


def test_a_trim_is_recorded_in_the_ledger_with_the_run(tmp_path: Path) -> None:
    led = _Ledger()
    r, _ = _router(tmp_path, ledger=led)                                    # type: ignore[arg-type]
    r.society_view = lambda sym: (0.5, "run-7")
    _submit(r, _intent())
    trims = [p for e, p in led.rows if e == "RISK_TRIMMED"]
    assert trims and trims[0]["from_shares"] == 100 and trims[0]["to_shares"] == 50
    assert trims[0]["reason"] == "society_influence=0.500" and trims[0]["society_run_id"] == "run-7"


# ---- SocietyView -------------------------------------------------------------------------------

P = AdapterParams(a=0.5, b=0.3, c=0.2, d_ref=0.5, floor=0.25)           # TEST VALUES ONLY
T0 = datetime(2026, 10, 3, 12, tzinfo=UTC)


def _seed_result() -> tuple[SimSeed, SimResult]:
    seed = SimSeed(run_id="abc123def456", created_at=T0.isoformat(), as_of=T0.isoformat(),
                   symbols=["BTC/USD", "ETH/USD"], snapshot={}, withheld={},
                   budget=RunBudget(max_usd=1, max_agents=5, max_steps=2))
    res = SimResult(run_id="abc123def456", finished_at=T0.isoformat(), completed_steps=2, agents=5,
                    model="m", spend_usd=0.1, input_tokens=1, output_tokens=1, stopped_by="steps",
                    stances=[Stance(symbol="BTC/USD", mean=-0.8, dispersion=0.0, agents=5),
                             Stance(symbol="ETH/USD", mean=0.6, dispersion=0.0, agents=5)])
    return seed, res


def test_view_maps_stances_through_the_adapter() -> None:
    seed, res = _seed_result()
    v = SocietyView.from_result(seed, res, P, max_age_h=24.0, min_agents=3, clock=lambda: T0 + timedelta(hours=1))
    assert v("BTC/USD") == (pytest.approx(0.6), "abc123def456")       # 1 - 0.5 * 0.8
    assert v("ETH/USD") == (1.0, "abc123def456")                       # constructive: unchanged
    assert v("SOL/USD") is None                                        # not covered by the run


def test_a_stale_or_future_view_goes_silent() -> None:
    seed, res = _seed_result()
    stale = SocietyView.from_result(seed, res, P, max_age_h=24.0, min_agents=3, clock=lambda: T0 + timedelta(hours=25))
    future = SocietyView.from_result(seed, res, P, max_age_h=24.0, min_agents=3, clock=lambda: T0 - timedelta(hours=1))
    assert stale("BTC/USD") is None and future("BTC/USD") is None


def test_view_refuses_a_foreign_result_and_a_bad_ttl() -> None:
    seed, res = _seed_result()
    other = seed.model_copy(update={"run_id": "zzzzzzzzzzzz"})
    with pytest.raises(ValueError, match="does not belong"):
        SocietyView.from_result(other, res, P, max_age_h=24.0, min_agents=3)
    for bad in (0.0, -1.0, float("nan")):
        with pytest.raises(ValueError):
            SocietyView(run_id="r", finished_at=T0, influence={}, max_age_h=bad)


def test_end_to_end_view_into_router(tmp_path: Path) -> None:
    seed, res = _seed_result()
    r, b = _router(tmp_path)
    r.society_view = SocietyView.from_result(seed, res, P, max_age_h=24.0, min_agents=3,
                                             clock=lambda: T0 + timedelta(minutes=5))
    _submit(r, _intent())                                                   # BTC/USD adverse: x0.6
    assert b.placed[0].totalQuantity == 60
    row = _journal_rows(tmp_path)[0]
    assert row["society_run_id"] == "abc123def456" and row["society_influence"] == pytest.approx(0.6)


def test_a_stance_from_too_few_agents_is_not_a_society_view() -> None:
    """The 2026-10-03 database held one post by one agent; a readout over that must not size orders."""
    seed, res = _seed_result()
    res = res.model_copy(update={"stances": [
        Stance(symbol="BTC/USD", mean=-0.9, dispersion=0.0, agents=1),      # a society of one
        Stance(symbol="ETH/USD", mean=-0.9, dispersion=0.0, agents=4)]})
    v = SocietyView.from_result(seed, res, P, max_age_h=24.0, min_agents=3,
                                clock=lambda: T0 + timedelta(minutes=1))
    assert v("BTC/USD") is None                                  # ignored, however adverse
    assert v("ETH/USD") is not None and v("ETH/USD")[0] < 1.0


def test_min_agents_is_required_and_must_be_positive() -> None:
    seed, res = _seed_result()
    with pytest.raises(TypeError):
        SocietyView.from_result(seed, res, P, max_age_h=24.0)               # type: ignore[call-arg]
    with pytest.raises(ValueError, match="min_agents"):
        SocietyView.from_result(seed, res, P, max_age_h=24.0, min_agents=0)
