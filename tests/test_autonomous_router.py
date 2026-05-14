from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading_live_claude.brokers.models import OrderAction, Quote
from trading_live_claude.execution.router import AutonomousNotEnabled, OrderIntent, Router


class _StubBroker:
    name = "stub"

    def __init__(self) -> None:
        self.placed: list = []

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
        order.id = 7
        return order

    def cancel_order(self, *_, **__):
        pass


def _intent(shares: int = 10, entry: float = 100.0) -> OrderIntent:
    return OrderIntent(
        symbol="AAPL",
        action=OrderAction.BUY,
        shares=shares,
        entry=entry,
        stop=entry * 0.96,
        target=entry * 1.08,
        strategy="test",
        risk_dollars=shares * entry * 0.04,
        account_number="PAPER-001",
        symbolId=1,
    )


def test_autonomous_requires_env_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTONOMOUS_ENABLED", raising=False)
    with pytest.raises(AutonomousNotEnabled):
        Router.build_default(
            mode="autonomous",
            broker=_StubBroker(),
            state_dir=tmp_path,
            daily_max_trades=5,
            daily_max_notional_usd=10_000,
        )


def test_autonomous_with_flag_constructs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTONOMOUS_ENABLED", "true")
    router = Router.build_default(
        mode="autonomous",
        broker=_StubBroker(),
        state_dir=tmp_path,
        daily_max_trades=5,
        daily_max_notional_usd=10_000,
    )
    assert router.mode == "autonomous"
    assert router.daily_budget is not None


def test_autonomous_respects_daily_trade_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTONOMOUS_ENABLED", "true")
    # Pre-populate orders.jsonl with 2 accepted orders today.
    now = datetime.now(UTC).isoformat()
    (tmp_path / "orders.jsonl").write_text(
        "\n".join(
            json.dumps({"ts": now, "shares": 10, "entry": 100.0, "accepted": True, "symbol": "X"})
            for _ in range(2)
        )
        + "\n",
        encoding="utf-8",
    )
    router = Router.build_default(
        mode="autonomous",
        broker=_StubBroker(),
        state_dir=tmp_path,
        daily_max_trades=2,
        daily_max_notional_usd=100_000,
    )
    out = router.submit(_intent(), equity=100_000, existing_risk=0, open_positions=0)
    assert out is None
    rejected = (tmp_path / "rejected.jsonl").read_text(encoding="utf-8")
    assert "trade cap" in rejected


def test_autonomous_respects_daily_notional_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTONOMOUS_ENABLED", "true")
    router = Router.build_default(
        mode="autonomous",
        broker=_StubBroker(),
        state_dir=tmp_path,
        daily_max_trades=99,
        daily_max_notional_usd=500.0,  # very tight notional
    )
    out = router.submit(_intent(shares=10, entry=100.0), equity=100_000, existing_risk=0, open_positions=0)
    assert out is None


def test_autonomous_normal_path_places_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTONOMOUS_ENABLED", "true")
    broker = _StubBroker()
    router = Router.build_default(
        mode="autonomous",
        broker=broker,
        state_dir=tmp_path,
        daily_max_trades=99,
        daily_max_notional_usd=999_999,
    )
    out = router.submit(_intent(shares=5, entry=100.0), equity=100_000, existing_risk=0, open_positions=0)
    assert out is not None
    assert broker.placed
