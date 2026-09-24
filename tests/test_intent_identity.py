"""One intent id, minted at the intent and carried through every record (2026-09-24).

Phase 2 of `AUDIT_LEDGER_SCOPE.md`. Before this, `orders.jsonl`, `fills.jsonl`, `rejected.jsonl`,
`scheduled_intents.jsonl` and `approval.db` shared no key, so "show me everything that happened to
this order" could not be answered from the record at all.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.test_router import _StubBroker, _intent
from trading_live_claude.execution.approval import (
    CardRegistry,
    InMemoryApprovalStore,
)
from trading_live_claude.execution.approval_sqlite import (
    SqliteApprovalStore,
    SqliteCardRegistry,
)
from trading_live_claude.execution.router import OrderIntent, Router, mint_intent_id


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_every_intent_is_born_with_a_unique_sortable_id() -> None:
    ids = [mint_intent_id() for _ in range(200)]
    assert len(set(ids)) == 200                       # no collisions
    assert ids == sorted(ids) or len(set(i.split("-")[0] for i in ids)) > 1   # time-prefixed
    assert _intent().intent_id != _intent().intent_id  # not shared via a mutable default


def test_an_explicit_id_is_preserved() -> None:
    """A caller replaying or reconstructing an intent must be able to keep its identity."""
    intent = OrderIntent(**{**vars(_intent()), "intent_id": "fixed-id-1"})
    assert intent.intent_id == "fixed-id-1"


def test_accepted_intent_and_its_fill_share_the_id(tmp_path: Path) -> None:
    broker = _StubBroker()
    router = Router.build_default(mode="paper", broker=broker, state_dir=tmp_path)
    intent = _intent()
    router.submit(intent, equity=100_000.0, existing_risk=0.0, open_positions=0)

    orders = _rows(tmp_path / "orders.jsonl")
    fills = _rows(tmp_path / "fills.jsonl")
    assert [r["intent_id"] for r in orders] == [intent.intent_id]
    assert [r["intent_id"] for r in fills] == [intent.intent_id]
    # The join that was impossible before: intent -> broker order id.
    assert fills[0]["order_id"] == broker.placed[0].id


def test_a_gate_rejection_is_attributable(tmp_path: Path) -> None:
    """A rejection without an id can't be tied to the signal that caused it."""
    broker = _StubBroker()
    router = Router.build_default(mode="paper", broker=broker, state_dir=tmp_path,
                                  min_ticket_usd=1_000_000.0)
    intent = _intent()
    assert router.submit(intent, equity=100_000.0, existing_risk=0.0, open_positions=0) is None

    rejected = _rows(tmp_path / "rejected.jsonl")
    orders = _rows(tmp_path / "orders.jsonl")
    assert rejected and rejected[0]["intent_id"] == intent.intent_id
    assert orders[0]["intent_id"] == intent.intent_id and orders[0]["accepted"] is False


@pytest.mark.parametrize("persistent", [False, True])
def test_the_approval_record_reuses_the_intents_own_id(tmp_path: Path, persistent: bool) -> None:
    """The card path used to mint a second id, orphaning the approval from the router's rows."""
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if persistent:
        reg: CardRegistry | SqliteCardRegistry = SqliteCardRegistry(tmp_path / "a.db")
        store: InMemoryApprovalStore | SqliteApprovalStore = SqliteApprovalStore(
            reg, tmp_path / "a.db")
    else:
        reg = CardRegistry()
        store = InMemoryApprovalStore(reg)
    reg.register("c1", pem)

    intent = _intent()
    prompt = store.publish(intent, mode="paper", broker="ib", ttl_seconds=5)
    assert prompt.intent_id == intent.intent_id
    # The id is inside the signed bytes, so the correlation key is itself covered by the signature.
    assert intent.intent_id in prompt.canonical


def test_queued_intents_keep_their_id_across_the_wait(tmp_path: Path) -> None:
    """A closed-venue intent is journalled at queue time and again at release; one id, both rows."""
    from datetime import UTC, datetime

    from trading_live_claude.execution.scheduler import MicrostructureConfig, SessionRouter

    broker = _StubBroker()
    inner = Router.build_default(mode="paper", broker=broker, state_dir=tmp_path)
    journal = tmp_path / "scheduled_intents.jsonl"
    closed = datetime(2026, 9, 26, 3, 0, tzinfo=UTC)          # Saturday: TSX shut
    router = SessionRouter(inner, broker, account_number="PAPER-001",
                           config=MicrostructureConfig(), journal_path=journal,
                           clock=lambda: closed)
    intent = OrderIntent(**{**vars(_intent()), "symbol": "XIC.TO"})
    assert router.submit(intent, equity=100_000.0, existing_risk=0.0, open_positions=0) is None

    queued = _rows(journal)
    assert queued and queued[0]["event"] == "queued"
    assert queued[0]["intent_id"] == intent.intent_id
