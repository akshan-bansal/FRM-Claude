"""Tests for the PaperBroker journal upgrade — session_id, paper_orders.jsonl, paper_equity.csv."""
from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading_live_claude.brokers.base import Broker, OrderRejected
from trading_live_claude.brokers.models import (
    Account, Candle, Order, OrderAction, OrderType, Position, Quote,
)
from trading_live_claude.brokers.paper import PaperBroker


class _StaticFeed(Broker):
    """Minimal broker stub — just enough for the paper broker to price fills."""

    name = "static-feed"

    def __init__(self, prices: dict[str, float]) -> None:
        self._prices = prices

    def accounts(self) -> list[Account]:
        return [Account(type="Feed", number="F", status="Active", isPrimary=True)]

    def positions(self, account_number: str) -> list[Position]:
        return []

    def quote(self, symbol: str) -> Quote:
        p = self._prices.get(symbol)
        if p is None:
            return Quote(symbol=symbol, symbolId=0, bidPrice=None, askPrice=None,
                         lastTradePrice=None)
        return Quote(symbol=symbol, symbolId=0, bidPrice=p - 0.01, askPrice=p + 0.01,
                     lastTradePrice=p)

    def quotes(self, symbols: list[str]) -> list[Quote]:
        return [self.quote(s) for s in symbols]

    def candles(self, symbol: str, start: datetime, end: datetime,
                interval: str = "OneDay") -> list[Candle]:
        return []

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        return 0.0

    def place_order(self, order: Order) -> Order:  # pragma: no cover - stub never called
        raise NotImplementedError

    def cancel_order(self, account_number: str, order_id: int) -> None:  # pragma: no cover
        raise NotImplementedError


def _order(symbol: str, action: OrderAction, qty: int) -> Order:
    return Order(symbol=symbol, symbolId=0, totalQuantity=qty, action=action,
                 orderType=OrderType.MARKET)


def test_session_id_is_generated_at_construction_and_stable_across_orders(tmp_path: Path) -> None:
    """One broker instance = one session id; the id is a uuid4 hex string."""
    pb = PaperBroker(feed=_StaticFeed({"EQB.TO": 100.0}), starting_equity=10_000.0,
                     journal_dir=tmp_path)
    sid = pb.session_id
    assert len(sid) == 32 and all(c in "0123456789abcdef" for c in sid)
    pb.place_order(_order("EQB.TO", OrderAction.BUY, 10))
    pb.place_order(_order("EQB.TO", OrderAction.BUY, 5))
    # Every row in every journal must carry the SAME session id.
    fills = [json.loads(line) for line in (tmp_path / "paper_fills.jsonl").read_text().splitlines()]
    orders = [json.loads(line) for line in (tmp_path / "paper_orders.jsonl").read_text().splitlines()]
    assert all(f["session_id"] == sid for f in fills)
    assert all(o["session_id"] == sid for o in orders)
    with (tmp_path / "paper_equity.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert all(r["session_id"] == sid for r in rows)


def test_two_instances_do_not_share_a_session_id(tmp_path: Path) -> None:
    """Regression on the 2026-08-31 stacking bug — the whole point of session_id."""
    a = PaperBroker(feed=_StaticFeed({}), journal_dir=tmp_path)
    b = PaperBroker(feed=_StaticFeed({}), journal_dir=tmp_path)
    assert a.session_id != b.session_id


def test_intent_is_journaled_even_when_the_fill_is_rejected(tmp_path: Path) -> None:
    """The old journal was silent on rejects; item 5's whole point is that they are on the record."""
    pb = PaperBroker(feed=_StaticFeed({}), journal_dir=tmp_path)   # no price -> reject
    with pytest.raises(OrderRejected):
        pb.place_order(_order("NONE", OrderAction.BUY, 1))

    orders_path = tmp_path / "paper_orders.jsonl"
    fills_path = tmp_path / "paper_fills.jsonl"
    assert orders_path.exists() and not fills_path.exists()

    row = json.loads(orders_path.read_text().splitlines()[0])
    assert row["symbol"] == "NONE"
    assert row["accepted"] is False
    assert row["rejected_reasons"] == ["no_reference_price"]
    assert row["ref_price"] is None


def test_equity_csv_carries_peak_and_drawdown_and_reprints_no_second_header(tmp_path: Path) -> None:
    """peak_equity monotone, drawdown_pct sane, header appears exactly once even on reopens."""
    feed = _StaticFeed({"EQB.TO": 100.0})
    pb = PaperBroker(feed=feed, starting_equity=10_000.0, journal_dir=tmp_path,
                     slippage_bps=0.0, commission_per_trade=0.0)
    pb.place_order(_order("EQB.TO", OrderAction.BUY, 50))          # spend $5000
    # simulate an adverse mark by re-fetching quote from a lower price on the next fill
    feed._prices["EQB.TO"] = 90.0
    pb.place_order(_order("EQB.TO", OrderAction.BUY, 10))          # more at the lower price

    with (tmp_path / "paper_equity.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2

    peaks = [float(r["peak_equity"]) for r in rows]
    assert peaks == sorted(peaks), "peak_equity must be monotone non-decreasing"

    dd_final = float(rows[-1]["drawdown_pct"])
    equity_final = float(rows[-1]["equity"])
    peak_final = peaks[-1]
    assert dd_final == pytest.approx((peak_final - equity_final) / peak_final, rel=1e-6)

    # Reopen the broker (new session) against the same dir. The header must not repeat.
    pb2 = PaperBroker(feed=feed, starting_equity=10_000.0, journal_dir=tmp_path)
    pb2.place_order(_order("EQB.TO", OrderAction.BUY, 5))
    text = (tmp_path / "paper_equity.csv").read_text()
    assert text.count("peak_equity") == 1


def test_realized_pnl_accrues_on_a_closing_fill(tmp_path: Path) -> None:
    """A round-trip long that closes at a profit records that profit in realized_pnl."""
    feed = _StaticFeed({"EQB.TO": 100.0})
    pb = PaperBroker(feed=feed, starting_equity=10_000.0, journal_dir=tmp_path,
                     slippage_bps=0.0, commission_per_trade=0.0)
    pb.place_order(_order("EQB.TO", OrderAction.BUY, 10))
    feed._prices["EQB.TO"] = 110.0
    pb.place_order(_order("EQB.TO", OrderAction.SELL, 10))

    with (tmp_path / "paper_equity.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    # 10 sh * (110 - 100) = $100 realized on the close.
    assert float(rows[-1]["realized_pnl"]) == pytest.approx(100.0)
    # Position is closed -> positions_value == 0, unrealized == 0.
    assert float(rows[-1]["positions_value"]) == pytest.approx(0.0)
    assert float(rows[-1]["unrealized_pnl"]) == pytest.approx(0.0)


def test_orders_journal_records_ref_price_and_symbol_shape(tmp_path: Path) -> None:
    """Downstream analysis needs to see what the intent WAS, not just that it was accepted."""
    pb = PaperBroker(feed=_StaticFeed({"QQQ": 715.0}), journal_dir=tmp_path,
                     slippage_bps=0.0, commission_per_trade=0.0)
    pb.place_order(_order("QQQ", OrderAction.BUY, 3))
    row = json.loads((tmp_path / "paper_orders.jsonl").read_text().splitlines()[0])
    assert row["symbol"] == "QQQ" and row["action"] == "Buy" and row["shares"] == 3
    assert row["accepted"] is True
    assert row["rejected_reasons"] == []
    assert row["ref_price"] == pytest.approx(715.0)
    # order_id is stamped on the intent, matching the fill.
    fill = json.loads((tmp_path / "paper_fills.jsonl").read_text().splitlines()[0])
    assert row["order_id"] == fill["order_id"]


def test_journal_is_a_no_op_when_no_journal_dir_is_configured() -> None:
    """A broker used without a journal_dir must still fill orders and not crash."""
    pb = PaperBroker(feed=_StaticFeed({"EQB.TO": 100.0}), starting_equity=10_000.0)
    filled = pb.place_order(_order("EQB.TO", OrderAction.BUY, 1))
    # Order counter is a class-level itertools.count that persists across instances; the exact id
    # depends on other tests, so only assert that one WAS stamped and that the fill was recorded.
    assert filled.id is not None
    assert len(pb.fills) == 1
