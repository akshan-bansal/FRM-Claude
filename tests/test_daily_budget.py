from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_live_claude.execution.daily_budget import DailyBudget


def _write_order(path: Path, when: datetime, shares: int, entry: float, accepted: bool = True) -> None:
    row = {
        "ts": when.isoformat(),
        "shares": shares,
        "entry": entry,
        "accepted": accepted,
        "symbol": "AAPL",
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def test_empty_state_returns_zero(tmp_path: Path) -> None:
    snap = DailyBudget(tmp_path, max_trades_per_day=5, max_notional_per_day_usd=1000).snapshot()
    assert snap.trades_today == 0
    assert snap.notional_today_usd == 0.0
    assert snap.trades_remaining == 5


def test_counts_today_only(tmp_path: Path) -> None:
    orders = tmp_path / "orders.jsonl"
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    _write_order(orders, yesterday, shares=10, entry=100.0)
    _write_order(orders, now, shares=20, entry=50.0)
    _write_order(orders, now, shares=5, entry=200.0)
    snap = DailyBudget(tmp_path, max_trades_per_day=5, max_notional_per_day_usd=10_000).snapshot()
    assert snap.trades_today == 2
    assert snap.notional_today_usd == pytest.approx(20 * 50.0 + 5 * 200.0)


def test_rejected_orders_not_counted(tmp_path: Path) -> None:
    orders = tmp_path / "orders.jsonl"
    now = datetime.now(UTC)
    _write_order(orders, now, shares=10, entry=100.0, accepted=False)
    snap = DailyBudget(tmp_path).snapshot()
    assert snap.trades_today == 0


def test_admits_under_cap(tmp_path: Path) -> None:
    snap = DailyBudget(tmp_path, max_trades_per_day=3, max_notional_per_day_usd=1000).snapshot()
    ok, _ = snap.admits(additional_notional_usd=500.0)
    assert ok


def test_refuses_over_trade_cap(tmp_path: Path) -> None:
    orders = tmp_path / "orders.jsonl"
    now = datetime.now(UTC)
    for _ in range(3):
        _write_order(orders, now, shares=1, entry=1.0)
    snap = DailyBudget(tmp_path, max_trades_per_day=3, max_notional_per_day_usd=99_999).snapshot()
    ok, reason = snap.admits(additional_notional_usd=1.0)
    assert not ok
    assert "trade cap" in reason


def test_refuses_over_notional_cap(tmp_path: Path) -> None:
    orders = tmp_path / "orders.jsonl"
    now = datetime.now(UTC)
    _write_order(orders, now, shares=10, entry=500.0)  # notional 5000
    snap = DailyBudget(tmp_path, max_trades_per_day=99, max_notional_per_day_usd=6000).snapshot()
    ok, reason = snap.admits(additional_notional_usd=1500.0)
    assert not ok
    assert "notional cap" in reason
