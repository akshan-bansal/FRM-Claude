from __future__ import annotations

from pathlib import Path
from typing import Any

from trading_live_claude.analysis.universe import (
    CRYPTO_SLEEVE,
    WALK_FORWARD_VALIDATED,
    crypto_sleeve_for,
    crypto_sleeve_symbols,
)
from trading_live_claude.brokers.models import OrderAction, Quote
from trading_live_claude.execution.router import OrderIntent, Router
from trading_live_claude.strategies import STRATEGIES


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

    def candles(self, *args: Any, **kwargs: Any) -> list:
        return []

    def equity(self, _: str) -> float:
        return 100_000.0

    def place_order(self, order: Any) -> Any:
        self.placed.append(order)
        order.id = 1
        return order

    def cancel_order(self, *_: Any, **__: Any) -> None:
        pass


def test_crypto_sleeve_is_well_formed() -> None:
    assert set(crypto_sleeve_symbols()) == {"BTC/USD", "XMR/USD", "XRP/USD", "XLM/USD", "LINK/USD", "ETH/USD"}
    for sym, e in CRYPTO_SLEEVE.items():
        assert e.symbol == sym and "/" in sym
        assert e.pair.endswith("USD")
        assert e.strategy in STRATEGIES, e.strategy
        assert e.asset_class == "crypto"
        assert e.tier == "screened"          # provisional, not walk-forward-validated
    assert crypto_sleeve_for("BTC/USD").strategy == "macd"
    assert crypto_sleeve_for("NOT/USD") is None


def test_crypto_sleeve_kept_separate_from_validated_pool() -> None:
    """The crypto sleeve is screened, not validated — none of it leaks into WALK_FORWARD_VALIDATED."""
    for sym in crypto_sleeve_symbols():
        assert sym not in WALK_FORWARD_VALIDATED


def test_crypto_intent_routes_through_the_paper_router(tmp_path: Path) -> None:
    """A crypto order intent passes through the same Router risk gates as equities (paper mode)."""
    broker = _StubBroker()
    router = Router.build_default(mode="paper", broker=broker, state_dir=tmp_path)
    intent = OrderIntent(
        symbol="BTC/USD", action=OrderAction.BUY, shares=10, entry=100.0, stop=96.0, target=108.0,
        strategy="macd", risk_dollars=400.0, account_number="PAPER-001", symbolId=1,
    )
    out = router.submit(intent, equity=100_000, existing_risk=0, open_positions=0)
    assert out is not None                    # cleared the gates and was placed (paper)
    assert broker.placed and broker.placed[0].symbol == "BTC/USD"
