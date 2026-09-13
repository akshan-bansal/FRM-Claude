"""WYSIWYS checks in the laptop card simulator (mirrors the firmware logic)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from trading_live_claude.brokers.models import OrderAction
from trading_live_claude.execution.approval import CardRegistry, InMemoryApprovalStore
from trading_live_claude.execution.router import OrderIntent

_SIM_PATH = Path(__file__).resolve().parent.parent / "scripts" / "approval_card_sim.py"
_spec = importlib.util.spec_from_file_location("approval_card_sim", _SIM_PATH)
assert _spec and _spec.loader
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)


@pytest.fixture
def prompt() -> dict:
    store = InMemoryApprovalStore(CardRegistry())
    intent = OrderIntent(
        symbol="XIC.TO", action=OrderAction.BUY, shares=10, entry=31.05,
        stop=30.5, target=32.0, strategy="test", risk_dollars=5.5,
        account_number="PAPER-001", symbolId=1,
    )
    return store.publish(intent, mode="paper", broker="ib", ttl_seconds=30).to_dict()


def test_genuine_prompt_parses_to_the_signed_fields(prompt: dict) -> None:
    signed = sim.parse_canonical(prompt)
    assert signed is not None
    assert signed["broker"] == "ib"
    assert signed["symbol"] == "XIC.TO"
    assert int(signed["shares"]) == 10
    assert signed["intent_id"] == prompt["intent_id"]
    assert signed["nonce"] == prompt["nonce"]


def test_display_comes_from_signed_bytes_not_json_fields(prompt: dict) -> None:
    signed = sim.parse_canonical(prompt)
    assert signed is not None
    spoofed = {**prompt, "symbol": "TSLA", "shares": 1, "notional_usd": 1.0, "broker": "kraken"}
    line = sim.render_prompt(spoofed, signed)
    assert "XIC.TO" in line and "[IB]" in line and "10sh" in line
    assert "TSLA" not in line and "KRAKEN" not in line


def test_fractional_crypto_quantity_is_displayed_as_signed() -> None:
    store = InMemoryApprovalStore(CardRegistry())
    intent = OrderIntent(
        symbol="BTC/USD", action=OrderAction.BUY, shares=0.05, entry=42000.0,  # type: ignore[arg-type]
        stop=41000.0, target=44000.0, strategy="macd", risk_dollars=50.0,
        account_number="K1", symbolId=1,
    )
    p = store.publish(intent, mode="paper", broker="kraken", ttl_seconds=30).to_dict()
    signed = sim.parse_canonical(p)
    assert signed is not None and signed["shares"] == "0.05"
    assert "0.05sh" in sim.render_prompt(p, signed)


def test_canonical_bound_to_another_intent_is_refused(prompt: dict) -> None:
    assert sim.parse_canonical({**prompt, "intent_id": "some-other-intent"}) is None


@pytest.mark.parametrize("canonical", [
    "",
    "ib|Buy|XIC.TO|10",
    "ib|Buy|XIC.TO|10|31.0500|310.50|PAPER-001|iid|nonce|extra",
    "ib|Buy||10|31.0500|310.50|PAPER-001|iid|nonce",
])
def test_malformed_canonical_is_refused(prompt: dict, canonical: str) -> None:
    assert sim.parse_canonical({**prompt, "canonical": canonical, "intent_id": "iid"}) is None
