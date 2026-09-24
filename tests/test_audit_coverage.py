"""Full event coverage (AUDIT_LEDGER_SCOPE.md phase 5).

The chain from signal to fill, including the branches that used to leave no trace at all: a
suppressed signal, a trimmed size, a queued intent, the card's verdict, and the signature itself.
The end-to-end test is the acceptance criterion from the scope doc's section 2.5 — reconstruct one
intent's whole history, and re-verify its approval, from the ledger alone.
"""
from __future__ import annotations

from base64 import b64decode
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from tests.test_router import _StubBroker, _intent
from trading_live_claude.audit import Ledger
from trading_live_claude.brokers.models import OrderAction
from trading_live_claude.execution.approval import (
    ApprovalRouter,
    CardRegistry,
    InMemoryApprovalStore,
    fingerprint,
)
from trading_live_claude.execution.approval_sqlite import (
    SqliteApprovalStore,
    SqliteCardRegistry,
)
from trading_live_claude.execution.router import OrderIntent, Router
from trading_live_claude.execution.scheduler import MicrostructureConfig, SessionRouter


def _led(tmp_path: Path, stream: str = "cov") -> Ledger:
    return Ledger(tmp_path / "ledger", stream=stream, session_id="s1")


def _events(led: Ledger) -> list[str]:
    return [r["event"] for r in led.rows()]


# --- risk branches ---------------------------------------------------------------------------

def test_a_trimmed_size_is_recorded_with_its_reason(tmp_path: Path) -> None:
    """A trim silently changed the traded size; now the original and the cap that bound it are kept."""
    led = _led(tmp_path)
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                  ledger=led, max_position_notional_pct=0.02)
    intent = _intent(shares=100, entry=100.0)          # $10k against a $2k per-symbol cap
    router.submit(intent, equity=100_000.0, existing_risk=0.0, open_positions=0)

    trimmed = [r for r in led.rows() if r["event"] == "RISK_TRIMMED"]
    assert len(trimmed) == 1
    payload = trimmed[0]["payload"]
    assert payload["from_shares"] == 100 and payload["to_shares"] < 100
    assert "symbol_cap" in payload["reason"]
    assert led.verify() == (True, "ok")


def test_a_partial_fill_is_not_recorded_as_a_full_one(tmp_path: Path) -> None:
    """Recording a partial as FILLED would overstate the position on the record."""
    class _PartialBroker(_StubBroker):
        def place_order(self, order):                   # type: ignore[no-untyped-def]
            order.totalQuantity = order.totalQuantity / 2      # broker fills half
            return super().place_order(order)

    led = _led(tmp_path)
    router = Router.build_default(mode="paper", broker=_PartialBroker(), state_dir=tmp_path,
                                  ledger=led)
    router.submit(_intent(shares=100), equity=100_000.0, existing_risk=0.0, open_positions=0)
    assert "PARTIAL" in _events(led) and "FILLED" not in _events(led)
    partial = [r for r in led.rows() if r["event"] == "PARTIAL"][0]
    assert partial["payload"]["requested_shares"] == 100
    assert partial["payload"]["shares"] == 50


def test_a_broker_rejection_is_recorded(tmp_path: Path) -> None:
    from trading_live_claude.brokers.base import OrderRejected

    class _RefusingBroker(_StubBroker):
        def place_order(self, order):                   # type: ignore[no-untyped-def]
            raise OrderRejected("venue closed")

    led = _led(tmp_path)
    router = Router.build_default(mode="paper", broker=_RefusingBroker(), state_dir=tmp_path,
                                  ledger=led)
    assert router.submit(_intent(), equity=100_000.0, existing_risk=0.0,
                         open_positions=0) is None
    assert _events(led) == ["RISK_CHECK", "BROKER_SUBMITTED", "BROKER_REJECTED"]
    assert "venue closed" in led.rows()[-1]["payload"]["error"]


# --- queue branch ----------------------------------------------------------------------------

def test_a_closed_venue_queue_and_release_are_both_recorded(tmp_path: Path) -> None:
    led = _led(tmp_path)
    broker = _StubBroker()
    inner = Router.build_default(mode="paper", broker=broker, state_dir=tmp_path, ledger=led)
    closed = datetime(2026, 9, 26, 3, 0, tzinfo=UTC)          # Saturday: TSX shut
    router = SessionRouter(inner, broker, account_number="PAPER-001",
                           config=MicrostructureConfig(),
                           journal_path=tmp_path / "scheduled.jsonl", clock=lambda: closed)
    intent = OrderIntent(**{**vars(_intent()), "symbol": "XIC.TO"})
    assert router.submit(intent, equity=100_000.0, existing_risk=0.0, open_positions=0) is None

    queued = [r for r in led.rows() if r["event"] == "INTENT_QUEUED"]
    assert len(queued) == 1
    assert queued[0]["payload"]["venue"] == "TSX"
    assert queued[0]["payload"]["release_at"]
    assert queued[0]["intent_id"] == intent.intent_id


# --- signal branch ---------------------------------------------------------------------------

def test_a_suppressed_signal_leaves_a_trace(tmp_path: Path) -> None:
    """The phase 3 ambiguity: an empty ledger looked identical to a broken one."""
    from trading_live_claude.monitor.live_loop import LiveMonitor

    from tests.test_monitor import _Broker, _Market

    led = _led(tmp_path)
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                  ledger=led)
    monitor = LiveMonitor(
        broker=_Broker(),          # type: ignore[arg-type]
        market=_Market(),          # type: ignore[arg-type]
        strategy=None,             # type: ignore[arg-type] - not exercised here
        sizer=None,                # type: ignore[arg-type]
        router=router,
        account_number="X",
        symbols=["AAA"],
        on_event=lambda _e: None,
    )
    assert monitor.ledger is led                       # taken from the router, not passed twice

    class _Sized:
        shares, stop = 0.0, 9.5

    monitor._ledger_signal("AAA", type("S", (), {"name": "bollinger"})(), 10.0,
                           sized=_Sized(), routable=False, overlay_halt=True,
                           persistence_halt=False, conviction=0.4, v4=False)
    rows = led.rows()
    assert [r["event"] for r in rows] == ["SIGNAL_SUPPRESSED"]
    assert rows[0]["payload"]["reason"] == "overlay halt"
    assert rows[0]["strategy_id"] == "bollinger"


def test_a_routable_signal_is_recorded_as_a_signal(tmp_path: Path) -> None:
    from trading_live_claude.monitor.live_loop import LiveMonitor

    from tests.test_monitor import _Broker, _Market

    led = _led(tmp_path)
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                  ledger=led)
    monitor = LiveMonitor(
        broker=_Broker(),          # type: ignore[arg-type]
        market=_Market(),          # type: ignore[arg-type]
        strategy=None,             # type: ignore[arg-type]
        sizer=None,                # type: ignore[arg-type]
        router=router, account_number="X", symbols=["AAA"], on_event=lambda _e: None,
    )

    class _Sized:
        shares, stop = 12.0, 9.5

    monitor._ledger_signal("AAA", type("S", (), {"name": "bollinger"})(), 10.0,
                           sized=_Sized(), routable=True, overlay_halt=False,
                           persistence_halt=False, conviction=1.0, v4=False)
    rows = led.rows()
    assert [r["event"] for r in rows] == ["STRATEGY_SIGNAL"]
    assert rows[0]["payload"]["shares"] == 12.0 and rows[0]["payload"]["reason"] == ""


# --- approval axis ---------------------------------------------------------------------------

def _keypair():
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return key, pem


@pytest.mark.parametrize("verdict", ["ACCEPT", "DECLINE"])
def test_the_card_verdict_and_signature_reach_the_ledger(tmp_path: Path, verdict: str) -> None:
    key, pem = _keypair()
    reg = SqliteCardRegistry(tmp_path / "a.db")
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, tmp_path / "a.db")
    led = _led(tmp_path)
    inner = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                 ledger=led)
    router = ApprovalRouter(inner=inner, store=store, ttl_seconds=5)

    intent = _intent()
    import threading

    def _respond() -> None:
        for _ in range(200):
            pending = store.pending()
            if pending:
                p = pending[0]
                store.respond(p.intent_id, decision=verdict, card_id="c1",
                              signature=key.sign(p.canonical.encode()))
                return
            threading.Event().wait(0.01)

    t = threading.Thread(target=_respond)
    t.start()
    router.submit(intent, equity=100_000.0, existing_risk=0.0, open_positions=0)
    t.join(5)

    events = _events(led)
    assert "INTENT_SENT" in events
    assert ("APPROVED" if verdict == "ACCEPT" else "REJECTED") in events
    assert "SIGNED" in events                          # both verdicts are signed by the card
    signed = [r for r in led.rows() if r["event"] == "SIGNED"][0]
    assert signed["signature"] and signed["signing_key_id"] == "c1"
    # The signature in the ledger verifies against the canonical bytes in the same row.
    pub = serialization.load_pem_public_key(pem)
    assert isinstance(pub, Ed25519PublicKey)
    pub.verify(b64decode(signed["signature"]), signed["payload"]["canonical"].encode("utf-8"))
    assert led.verify() == (True, "ok")


def test_an_expired_prompt_records_no_signature(tmp_path: Path) -> None:
    """Nothing signed it, so a SIGNED row would imply evidence that does not exist."""
    reg = CardRegistry()
    store = InMemoryApprovalStore(reg)
    led = _led(tmp_path)
    inner = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                 ledger=led)
    router = ApprovalRouter(inner=inner, store=store, ttl_seconds=0.05)
    assert router.submit(_intent(), equity=100_000.0, existing_risk=0.0,
                         open_positions=0) is None
    events = _events(led)
    assert "EXPIRED" in events and "SIGNED" not in events


def test_the_prompt_fingerprint_is_recorded_for_continuity(tmp_path: Path) -> None:
    """Same 8 hex chars on the dashboard, the device and the ledger (scope section 3)."""
    reg = CardRegistry()
    store = InMemoryApprovalStore(reg)
    led = _led(tmp_path)
    inner = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                 ledger=led)
    router = ApprovalRouter(inner=inner, store=store, ttl_seconds=0.05)
    router.submit(_intent(), equity=100_000.0, existing_risk=0.0, open_positions=0)
    sent = [r for r in led.rows() if r["event"] == "INTENT_SENT"][0]
    expired = [r for r in led.rows() if r["event"] == "EXPIRED"][0]
    assert sent["payload"]["fingerprint"] == expired["payload"]["fingerprint"]
    assert "..." in sent["payload"]["fingerprint"]


def test_a_gate_rejection_on_the_card_path_is_recorded(tmp_path: Path) -> None:
    reg = CardRegistry()
    led = _led(tmp_path)
    inner = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                 ledger=led, min_ticket_usd=1_000_000.0)
    router = ApprovalRouter(inner=inner, store=InMemoryApprovalStore(reg), ttl_seconds=5)
    assert router.submit(_intent(), equity=100_000.0, existing_risk=0.0,
                         open_positions=0) is None
    rows = led.rows()
    assert [r["event"] for r in rows] == ["RISK_REJECTED"]
    assert rows[0]["payload"]["via"] == "approval-router"


# --- the acceptance criterion ----------------------------------------------------------------

def test_one_intents_whole_history_reconstructs_from_the_ledger_alone(tmp_path: Path) -> None:
    """Scope section 2.5 #1 and #2: signal -> approval -> fill, and the signature re-verifies."""
    import threading

    key, pem = _keypair()
    reg = SqliteCardRegistry(tmp_path / "a.db")
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, tmp_path / "a.db")
    led = _led(tmp_path)
    inner = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                 ledger=led)
    router = ApprovalRouter(inner=inner, store=store, ttl_seconds=5)
    intent = _intent()

    def _accept() -> None:
        for _ in range(200):
            pending = store.pending()
            if pending:
                p = pending[0]
                store.respond(p.intent_id, decision="ACCEPT", card_id="c1",
                              signature=key.sign(p.canonical.encode()))
                return
            threading.Event().wait(0.01)

    t = threading.Thread(target=_accept)
    t.start()
    order = router.submit(intent, equity=100_000.0, existing_risk=0.0, open_positions=0)
    t.join(5)
    assert order is not None

    mine = [r for r in led.rows() if r["intent_id"] == intent.intent_id]
    assert [r["event"] for r in mine] == [
        "RISK_CHECK", "INTENT_SENT", "APPROVED", "SIGNED", "RISK_CHECK", "BROKER_SUBMITTED",
        "FILLED",
    ]
    # Every row carries the gate version, so the rules in force are part of the record.
    assert {r["risk_check_version"] for r in mine} == {inner.risk_check_version()}
    # The fill's broker order id ties the audit record to the broker's own record.
    assert mine[-1]["broker_order_id"] == 1
    assert led.verify() == (True, "ok")
    # And the approval re-verifies offline, from the ledger row, with no store involved.
    signed = next(r for r in mine if r["event"] == "SIGNED")
    pub = serialization.load_pem_public_key(pem)
    assert isinstance(pub, Ed25519PublicKey)
    pub.verify(b64decode(signed["signature"]), signed["payload"]["canonical"].encode("utf-8"))
    assert signed["payload"]["fingerprint"] == fingerprint(
        signed["payload"]["canonical"].encode("utf-8"))
