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
