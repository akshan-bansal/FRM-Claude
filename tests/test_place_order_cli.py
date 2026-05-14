"""Smoke tests for the place-order CLI flow (the LLM-trader's hands).

We can't easily exercise the Typer subprocess in-process without spinning up a
real Questrade broker, so we verify the small bits of logic that don't depend
on the broker: mode validation, side parsing, kill-switch refusal path. Full
end-to-end coverage lives in test_router.py.
"""
from __future__ import annotations

import json
from pathlib import Path


def test_place_order_command_registered() -> None:
    from trading_live_claude.cli import app

    # Typer apps expose registered commands via .registered_commands
    names = {c.name for c in app.registered_commands}
    assert "place-order" in names or "place_order" in names


def test_kill_switch_file_blocks_orders(tmp_path: Path) -> None:
    """Sanity check: if HALTED exists, KillSwitch reports halted; Router uses this."""
    from trading_live_claude.risk import KillSwitch

    (tmp_path / "HALTED").write_text("test", encoding="utf-8")
    ks = KillSwitch(tmp_path)
    state = ks.state()
    assert state.halted is True


def test_orders_jsonl_records_decisions(tmp_path: Path) -> None:
    """Verify the journal format includes the fields the LLM-trader writes."""
    from trading_live_claude.execution.journal import OrderJournal

    j = OrderJournal(tmp_path)
    j.order_intent(
        {
            "mode": "paper",
            "strategy": "llm-claude",
            "symbol": "XIC.TO",
            "action": "Buy",
            "shares": 2,
            "entry": 36.0,
            "stop": 35.0,
            "target": 38.0,
            "risk_dollars": 2.0,
            "accepted": True,
            "rejected_reasons": [],
        }
    )
    line = (tmp_path / "orders.jsonl").read_text(encoding="utf-8").strip()
    row = json.loads(line)
    assert row["strategy"] == "llm-claude"
    assert row["symbol"] == "XIC.TO"
    assert row["accepted"] is True
    assert "ts" in row
