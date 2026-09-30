"""Venue-priced costs (execution.venue_costs), the cost-aware size floor, and paper fills."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from trading_live_claude.brokers.models import Order, OrderAction, OrderType
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.execution.router import OrderIntent, Router
from trading_live_claude.execution.venue_costs import VenueCostModel


def test_questrade_is_a_flat_fee_per_fill() -> None:
    m = VenueCostModel.for_venue("questrade")
    assert m.cost_for(shares=100, price=50.0) == pytest.approx(4.95)
    assert m.cost_for(shares=10_000, price=50.0) == pytest.approx(4.95)   # size-independent


def test_kraken_is_a_percentage_of_notional() -> None:
    m = VenueCostModel.for_venue("kraken")
    # 26 bps taker: the 2026-09-23 LINK fill cost ~$0.48, not the $4.95 that was charged.
    assert m.cost_for(shares=14.55, price=12.7126) == pytest.approx(0.481, abs=0.01)
    assert m.cost_for(shares=1455, price=12.7126) == pytest.approx(48.1, abs=0.2)


def test_ib_is_per_share_with_a_minimum_and_a_cap() -> None:
    m = VenueCostModel.for_venue("ib")
    assert m.cost_for(shares=1000, price=50.0) == pytest.approx(5.00)      # 1000 x $0.005
    assert m.cost_for(shares=50, price=50.0) == pytest.approx(1.00)        # order minimum
    assert m.cost_for(shares=1000, price=0.20) == pytest.approx(2.00)      # capped at 1% of $200


def test_an_unknown_or_duck_typed_venue_gets_the_conservative_flat_fee() -> None:
    for venue in (None, "", "   ", object(), 7):
        m = VenueCostModel.for_venue(venue)
        assert m.cost_for(shares=100, price=50.0) == pytest.approx(4.95)


def test_round_trip_ratio_shows_why_a_small_flat_fee_ticket_cannot_pay_for_itself() -> None:
    qt = VenueCostModel.for_venue("questrade")
    assert qt.round_trip_cost_ratio(shares=14.55, price=12.7126) > 0.05    # $185 ticket: >5%
    assert qt.round_trip_cost_ratio(shares=1651, price=6.7584) < 0.003     # $11k ticket: <0.3%


def test_min_notional_is_size_dependent_only_where_the_fee_is_fixed() -> None:
    qt = VenueCostModel.for_venue("questrade")
    assert qt.min_notional_for(max_cost_ratio=0.005, price=10.0, shares=1) == pytest.approx(2475, abs=5)
    # Kraken's taker fee alone is 52 bps round trip, so a 50 bps ceiling fits at NO size —
    # the ceiling has to be venue-aware (or ~1%), which is the finding this encodes.
    kr = VenueCostModel.for_venue("kraken")
    assert kr.min_notional_for(max_cost_ratio=0.005, price=10.0, shares=1) == float("inf")
    assert kr.min_notional_for(max_cost_ratio=0.02, price=10.0, shares=1) == 0.0   # any size fits


# ---- the gate ----------------------------------------------------------------------------------

@dataclass
class _Q:
    mid: float
    lastTradePrice: float
    bidPrice: float | None = None
    askPrice: float | None = None


class _Feed:
    name = "fake"
    venue = "questrade"

    def quote(self, symbol: str) -> _Q:
        return _Q(mid=12.7126, lastTradePrice=12.7126)

    def positions(self, account_number: str) -> list[object]:
        return []

    def accounts(self) -> list[object]:
        return []

    def place_order(self, order: Order) -> Order:
        return order

    def cancel_order(self, *a: object, **k: object) -> None:
        pass


def _router(tmp_path: Path, ratio: float, venue: str = "questrade") -> Router:
    feed = _Feed()
    feed.venue = venue
    return Router.build_default(mode="paper", broker=feed, state_dir=tmp_path,  # type: ignore[arg-type]
                               cap_pct=1.0, max_open_positions=10, min_ticket_usd=1.0,
                               max_round_trip_cost_ratio=ratio)


def _buy(symbol: str, shares: float, price: float) -> OrderIntent:
    return OrderIntent(symbol=symbol, action=OrderAction.BUY, shares=shares, entry=price,
                       stop=price * 0.95, target=None, strategy="t", risk_dollars=1.0,
                       account_number="PAPER-001", symbolId=1)


def test_gate_rejects_a_ticket_whose_round_trip_cost_dwarfs_it(tmp_path: Path) -> None:
    r = _router(tmp_path, ratio=0.005)
    intent = _buy("LINK", 14.55, 12.7126)                       # the real 2026-09-23 fill: $185
    decision = r._gate(intent, equity=100_000, existing_risk=0.0, open_positions=0)
    assert not decision.accepted
    reason = "; ".join(decision.rejected_reasons)
    assert "round-trip cost" in reason and "questrade" in reason and "2,475" in reason


def test_gate_admits_a_ticket_that_carries_its_costs(tmp_path: Path) -> None:
    r = _router(tmp_path, ratio=0.005)
    intent = _buy("RSI.TO", 1651, 6.7584)                       # $11,158
    assert r._gate(intent, equity=100_000, existing_risk=0.0, open_positions=0).accepted


def test_the_floor_is_off_by_default(tmp_path: Path) -> None:
    r = _router(tmp_path, ratio=0.0)
    intent = _buy("LINK", 14.55, 12.7126)
    assert r._gate(intent, equity=100_000, existing_risk=0.0, open_positions=0).accepted


def test_exits_are_never_gated_on_cost(tmp_path: Path) -> None:
    r = _router(tmp_path, ratio=0.005)
    sell = OrderIntent(symbol="LINK", action=OrderAction.SELL, shares=14.55, entry=12.7126,
                       stop=14.0, target=None, strategy="t", risk_dollars=0.0,
                       account_number="PAPER-001", symbolId=1)
    assert r._gate(sell, equity=100_000, existing_risk=0.0, open_positions=1).accepted


# ---- paper fills -------------------------------------------------------------------------------

class _KrakenishFeed(_Feed):
    venue = "kraken"


def test_paper_fill_charges_the_venue_fee_not_a_flat_495(tmp_path: Path) -> None:
    pb = PaperBroker(feed=_KrakenishFeed(), starting_equity=100_000.0, journal_dir=tmp_path,  # type: ignore[arg-type]
                     slippage_bps=0.0)
    order = Order(symbol="LINK/USD", symbolId=1, action=OrderAction.BUY,
                  orderType=OrderType.MARKET, totalQuantity=14.55)
    filled = pb.place_order(order)
    assert filled.id is not None
    fill = pb.fills[-1]
    assert fill.commission == pytest.approx(0.481, abs=0.01)     # 26 bps, not $4.95
    assert pb.commission_per_trade == 0.0                        # no fixed part on a bps venue


def test_an_explicit_commission_still_wins(tmp_path: Path) -> None:
    pb = PaperBroker(feed=_KrakenishFeed(), starting_equity=100_000.0, journal_dir=tmp_path,  # type: ignore[arg-type]
                     slippage_bps=0.0, commission_per_trade=0.0)
    pb.place_order(Order(symbol="LINK/USD", symbolId=1, action=OrderAction.BUY,
                         orderType=OrderType.MARKET, totalQuantity=14.55))
    assert pb.fills[-1].commission == 0.0
    assert pb.commission_per_trade == 0.0


def test_a_questrade_feed_keeps_the_flat_fee(tmp_path: Path) -> None:
    pb = PaperBroker(feed=_Feed(), starting_equity=100_000.0, journal_dir=tmp_path,  # type: ignore[arg-type]
                     slippage_bps=0.0)
    pb.place_order(Order(symbol="AAA", symbolId=1, action=OrderAction.BUY,
                         orderType=OrderType.MARKET, totalQuantity=100))
    assert pb.fills[-1].commission == pytest.approx(4.95)
    assert pb.commission_per_trade == pytest.approx(4.95)
