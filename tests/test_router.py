from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trading_live_claude.brokers.models import OrderAction, Quote
from trading_live_claude.execution.router import (
    LIVE_CONFIRM_PHRASE,
    LiveModeNotConfirmed,
    OrderIntent,
    Router,
)


class _StubBroker:
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

    def candles(self, *args, **kwargs):  # pragma: no cover
        return []

    def equity(self, _: str) -> float:
        return 100_000.0

    def place_order(self, order):
        self.placed.append(order)
        order.id = 1
        return order

    def cancel_order(self, *_, **__):
        pass


def _intent(shares: int = 100, entry: float = 100.0, stop: float = 96.0, risk: float = 400.0) -> OrderIntent:
    return OrderIntent(
        symbol="AAPL",
        action=OrderAction.BUY,
        shares=shares,
        entry=entry,
        stop=stop,
        target=108.0,
        strategy="test",
        risk_dollars=risk,
        account_number="PAPER-001",
        symbolId=1,
    )


def test_live_mode_requires_confirmation(tmp_path: Path) -> None:
    broker = _StubBroker()
    with pytest.raises(LiveModeNotConfirmed):
        Router.build_default(mode="live", broker=broker, state_dir=tmp_path)


def test_live_mode_with_wrong_phrase(tmp_path: Path) -> None:
    broker = _StubBroker()
    with pytest.raises(LiveModeNotConfirmed):
        Router.build_default(mode="live", broker=broker, state_dir=tmp_path, live_confirmation="please")


def test_live_mode_with_correct_phrase(tmp_path: Path) -> None:
    broker = _StubBroker()
    router = Router.build_default(
        mode="live", broker=broker, state_dir=tmp_path, live_confirmation=LIVE_CONFIRM_PHRASE
    )
    assert router.mode == "live"


def test_router_rejects_when_kill_switch_tripped(tmp_path: Path) -> None:
    (tmp_path / "HALTED").write_text("test halt", encoding="utf-8")
    broker = _StubBroker()
    router = Router.build_default(mode="paper", broker=broker, state_dir=tmp_path)
    out = router.submit(_intent(), equity=100_000, existing_risk=0, open_positions=0)
    assert out is None
    assert (tmp_path / "rejected.jsonl").exists()


def test_router_rejects_when_heat_exceeded(tmp_path: Path) -> None:
    broker = _StubBroker()
    router = Router.build_default(mode="paper", broker=broker, state_dir=tmp_path, cap_pct=0.05)
    out = router.submit(_intent(risk=10_000), equity=100_000, existing_risk=0, open_positions=0)
    assert out is None


def test_router_rejects_when_max_positions(tmp_path: Path) -> None:
    broker = _StubBroker()
    router = Router.build_default(mode="paper", broker=broker, state_dir=tmp_path, max_open_positions=3)
    out = router.submit(_intent(), equity=100_000, existing_risk=0, open_positions=3)
    assert out is None


def test_router_rejects_zero_shares(tmp_path: Path) -> None:
    broker = _StubBroker()
    router = Router.build_default(mode="paper", broker=broker, state_dir=tmp_path)
    out = router.submit(_intent(shares=0), equity=100_000, existing_risk=0, open_positions=0)
    assert out is None


def test_router_rejects_invalid_stop_for_long(tmp_path: Path) -> None:
    broker = _StubBroker()
    router = Router.build_default(mode="paper", broker=broker, state_dir=tmp_path)
    out = router.submit(_intent(stop=101.0), equity=100_000, existing_risk=0, open_positions=0)
    assert out is None


def test_router_paper_places(tmp_path: Path) -> None:
    broker = _StubBroker()
    router = Router.build_default(mode="paper", broker=broker, state_dir=tmp_path)
    out = router.submit(_intent(), equity=100_000, existing_risk=0, open_positions=0)
    assert out is not None
    assert broker.placed
    assert (tmp_path / "fills.jsonl").exists()


def test_router_dry_run_skips_placement(tmp_path: Path) -> None:
    broker = _StubBroker()
    router = Router.build_default(mode="dry-run", broker=broker, state_dir=tmp_path)
    out = router.submit(_intent(), equity=100_000, existing_risk=0, open_positions=0)
    assert out is None
    assert not broker.placed


# --------------------------------------------------------------------------- #
# leverage headroom reserves the commission (2026-09-25)                       #
# --------------------------------------------------------------------------- #

class _FeeBroker(_StubBroker):
    """A broker that charges a flat fee, like PaperBroker."""
    commission_per_trade = 4.95


def _buy(symbol: str, shares: float, entry: float) -> OrderIntent:
    return OrderIntent(
        symbol=symbol, action=OrderAction.BUY, shares=shares, entry=entry,
        stop=entry * 0.96, target=entry * 1.08, strategy="test",
        risk_dollars=shares * entry * 0.04, account_number="PAPER-001", symbolId=1,
    )


def test_cost_reserve_is_zero_when_broker_exposes_no_fee(tmp_path: Path) -> None:
    r = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path)
    assert r._cost_reserve() == 0.0


def test_cost_reserve_reads_the_broker_fee(tmp_path: Path) -> None:
    r = Router.build_default(mode="paper", broker=_FeeBroker(), state_dir=tmp_path)
    assert r._cost_reserve() == 4.95


def test_leverage_headroom_leaves_room_for_the_fee(tmp_path: Path) -> None:
    """An order sized to exactly fill the leverage cap must be trimmed by the fee.

    Before 2026-09-25 the cap was notional-only, so a fill that consumed the whole headroom
    still debited commission on top and drove cash negative (-$5.24 / -$16.36 / -$29.79 on
    2026-09-17/18, with the cap otherwise binding at 1.000x).
    """
    equity = 100_000.0
    r = Router.build_default(mode="paper", broker=_FeeBroker(), state_dir=tmp_path,
                             max_gross_leverage=1.0, max_position_notional_pct=1.0,
                             cap_pct=1.0, min_ticket_usd=1.0)
    # Ask for exactly 100% of equity at $100/share.
    intent = _buy("AAA", 1000.0, 100.0)          # 1000 x $100 == exactly 100% of equity
    decision = r._gate(intent, equity=equity, existing_risk=0.0, open_positions=0)
    assert decision.accepted, decision.rejected_reasons
    # _gate trims the intent in place. Without the reserve this stayed at 1000 shares and the
    # $4.95 fee then overdrew cash; the reserve costs exactly the one share that did not fit.
    assert intent.shares == 999
    assert intent.shares * 100.0 + 4.95 <= equity + 1e-6


def test_cost_reserve_ignores_a_non_numeric_attribute(tmp_path: Path) -> None:
    """A duck-typed attribute must not be trusted to size a risk gate.

    Found 2026-09-25: `float(MagicMock())` is 1.0, so a broker wrapper that delegates through
    __getattr__ — or any test double — answered `commission_per_trade` with an object that
    float()'d to a dollar. That silently tightened the leverage headroom on fabricated input
    and broke test_trim_mode_gross_leverage_cap_trims_to_headroom (trimmed to 99, not 100).
    """
    class _Delegating(_StubBroker):
        def __getattr__(self, name: str) -> object:      # answers ANY attribute
            return object()

    r = Router.build_default(mode="paper", broker=_Delegating(), state_dir=tmp_path)
    assert r._cost_reserve() == 0.0

    class _BoolFee(_StubBroker):
        commission_per_trade = True                      # bool is an int subclass; reject it

    r2 = Router.build_default(mode="paper", broker=_BoolFee(), state_dir=tmp_path)
    assert r2._cost_reserve() == 0.0
