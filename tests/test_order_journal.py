"""Tests for execution/journal.py and the DailyBudget that reads it (added 2026-09-18)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading_live_claude.execution.daily_budget import DailyBudget
from trading_live_claude.execution.journal import OrderJournal


def _rows(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_each_record_type_goes_to_its_own_file_with_a_timestamp(tmp_path: Path) -> None:
    j = OrderJournal(tmp_path / "state")
    j.order_intent({"symbol": "XIC.TO", "accepted": True, "shares": 10, "entry": 30.0})
    j.rejected({"symbol": "XIC.TO", "reasons": ["kill-switch tripped"]})
    j.fill({"symbol": "XIC.TO", "order_id": 7})
    for name, key in (("orders.jsonl", "accepted"), ("rejected.jsonl", "reasons"), ("fills.jsonl", "order_id")):
        rows = _rows(tmp_path / "state" / name)
        assert len(rows) == 1 and key in rows[0]
        datetime.fromisoformat(rows[0]["ts"])          # parseable ISO timestamp


def test_explicit_timestamp_is_kept_and_rows_append(tmp_path: Path) -> None:
    j = OrderJournal(tmp_path)
    j.order_intent({"symbol": "A", "ts": "2026-01-01T00:00:00+00:00"})
    j.order_intent({"symbol": "B"})
    rows = _rows(tmp_path / "orders.jsonl")
    assert [r["symbol"] for r in rows] == ["A", "B"]
    assert rows[0]["ts"] == "2026-01-01T00:00:00+00:00"


def test_daily_budget_counts_only_todays_accepted_intents(tmp_path: Path) -> None:
    j = OrderJournal(tmp_path)
    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    j.order_intent({"accepted": True, "shares": 10, "entry": 100.0})                  # counts: $1,000
    j.order_intent({"accepted": True, "shares": 5, "entry": 20.0})                    # counts: $100
    j.order_intent({"accepted": False, "shares": 999, "entry": 999.0})                # rejected
    j.order_intent({"accepted": True, "shares": 50, "entry": 50.0, "ts": yesterday})  # not today
    (tmp_path / "orders.jsonl").open("a", encoding="utf-8").write("not json\n")       # tolerated
    snap = DailyBudget(tmp_path, max_trades_per_day=2, max_notional_per_day_usd=5_000).snapshot()
    assert snap.trades_today == 2
    assert abs(snap.notional_today_usd - 1_100.0) < 1e-9
    ok, reason = snap.admits(additional_notional_usd=100.0)
    assert not ok and "trade cap" in reason                    # 2 of 2 trades used
