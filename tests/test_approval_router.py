from __future__ import annotations

import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trading_live_claude.brokers.models import OrderAction, Quote
from trading_live_claude.execution.approval import (
    ApprovalRouter,
    CardRegistry,
    InMemoryApprovalStore,
    canonical_bytes,
)
from trading_live_claude.execution.router import OrderIntent, Router


# --------------------------------------------------------------------------- #
# fixtures / stubs                                                            #
# --------------------------------------------------------------------------- #

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

    def candles(self, *args, **kwargs):
        return []

    def equity(self, _: str) -> float:
        return 100_000.0

    def place_order(self, order):
        order.id = 4242
        self.placed.append(order)
        return order

    def cancel_order(self, *_, **__):
        pass


def _intent(shares: int = 10, entry: float = 100.0, stop: float | None = None) -> OrderIntent:
    return OrderIntent(
        symbol="AAPL",
        action=OrderAction.BUY,
        shares=shares,
        entry=entry,
        stop=stop if stop is not None else entry * 0.96,
        target=entry * 1.08,
        strategy="test",
        risk_dollars=shares * entry * 0.04,
        account_number="PAPER-001",
        symbolId=1,
    )


@pytest.fixture
def paper_router(tmp_path: Path) -> Router:
    return Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path)


@pytest.fixture
def card_keypair() -> tuple[Ed25519PrivateKey, bytes]:
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return key, pem


@pytest.fixture
def store(card_keypair) -> tuple[InMemoryApprovalStore, CardRegistry]:
    registry = CardRegistry()
    _, pem = card_keypair
    registry.register("card-001", pem)
    return InMemoryApprovalStore(registry), registry


# --------------------------------------------------------------------------- #
# tests                                                                       #
# --------------------------------------------------------------------------- #

def test_gate_failure_short_circuits_no_prompt(paper_router: Router, store):
    inner_store, _ = store
    approval = ApprovalRouter(paper_router, store=inner_store, ttl_seconds=5)
    # shares = 0 -> gate fails; store should never see a publish
    result = approval.submit(_intent(shares=0), equity=100_000, existing_risk=0, open_positions=0)
    assert result is None
    assert inner_store.pending() == []
    rejected = (paper_router.journal.rejected_path).read_text().strip().splitlines()
    assert len(rejected) == 1
    assert "intent.shares <= 0" in rejected[0]


def test_accept_signed_response_dispatches(paper_router: Router, store, card_keypair):
    key, _ = card_keypair
    inner_store, _ = store
    approval = ApprovalRouter(paper_router, store=inner_store, ttl_seconds=10)

    result_holder: dict = {}

    def _submit():
        result_holder["order"] = approval.submit(
            _intent(), equity=100_000, existing_risk=0, open_positions=0
        )

    t = threading.Thread(target=_submit)
    t.start()

    # wait for the prompt to appear
    for _ in range(100):
        pending = inner_store.pending()
        if pending:
            break
        threading.Event().wait(0.01)
    assert pending, "no prompt published"
    prompt = pending[0]

    sig = key.sign(prompt.canonical.encode("utf-8"))
    ok = inner_store.respond(prompt.intent_id, decision="ACCEPT", card_id="card-001", signature=sig)
    assert ok

    t.join(timeout=2)
    assert not t.is_alive()
    order = result_holder["order"]
    assert order is not None
    assert order.symbol == "AAPL"
    assert order.totalQuantity == 10


def test_decline_journals_and_does_not_place(paper_router: Router, store, card_keypair):
    key, _ = card_keypair
    inner_store, _ = store
    approval = ApprovalRouter(paper_router, store=inner_store, ttl_seconds=10)

    result_holder: dict = {}
    t = threading.Thread(
        target=lambda: result_holder.__setitem__(
            "order",
            approval.submit(_intent(), equity=100_000, existing_risk=0, open_positions=0),
        )
    )
    t.start()
    for _ in range(100):
        pending = inner_store.pending()
        if pending:
            break
        threading.Event().wait(0.01)
    prompt = pending[0]

    sig = key.sign(prompt.canonical.encode("utf-8"))
    inner_store.respond(prompt.intent_id, decision="DECLINE", card_id="card-001", signature=sig)
    t.join(timeout=2)
    assert result_holder["order"] is None
    rejected = paper_router.journal.rejected_path.read_text()
    assert "card:decline" in rejected


def test_expired_when_ttl_elapses(paper_router: Router, store):
    inner_store, _ = store
    approval = ApprovalRouter(paper_router, store=inner_store, ttl_seconds=0.05)

    result = approval.submit(_intent(), equity=100_000, existing_risk=0, open_positions=0)
    assert result is None
    rejected = paper_router.journal.rejected_path.read_text()
    assert "card:expired" in rejected


def test_bad_signature_is_rejected(paper_router: Router, store):
    inner_store, _ = store
    approval = ApprovalRouter(paper_router, store=inner_store, ttl_seconds=2)

    result_holder: dict = {}
    t = threading.Thread(
        target=lambda: result_holder.__setitem__(
            "order",
            approval.submit(_intent(), equity=100_000, existing_risk=0, open_positions=0),
        )
    )
    t.start()
    for _ in range(100):
        pending = inner_store.pending()
        if pending:
            break
        threading.Event().wait(0.01)
    prompt = pending[0]

    # Sign with a DIFFERENT key
    imposter = Ed25519PrivateKey.generate()
    bad_sig = imposter.sign(prompt.canonical.encode("utf-8"))
    ok = inner_store.respond(prompt.intent_id, decision="ACCEPT", card_id="card-001",
                             signature=bad_sig)
    assert ok is False
    # Prompt remains live; real card could still respond. Wait for TTL to expire.
    t.join(timeout=3)
    assert result_holder["order"] is None


def test_response_replay_rejected(paper_router: Router, store, card_keypair):
    key, _ = card_keypair
    inner_store, _ = store
    approval = ApprovalRouter(paper_router, store=inner_store, ttl_seconds=10)

    t = threading.Thread(
        target=lambda: approval.submit(_intent(), equity=100_000, existing_risk=0, open_positions=0)
    )
    t.start()
    for _ in range(100):
        pending = inner_store.pending()
        if pending:
            break
        threading.Event().wait(0.01)
    prompt = pending[0]

    sig = key.sign(prompt.canonical.encode("utf-8"))
    assert inner_store.respond(prompt.intent_id, decision="ACCEPT", card_id="card-001",
                               signature=sig) is True
    # Second call for the same intent must be refused (single-use).
    assert inner_store.respond(prompt.intent_id, decision="ACCEPT", card_id="card-001",
                               signature=sig) is False
    t.join(timeout=2)


def test_response_landing_during_verify_cannot_overwrite_verdict(
    store, card_keypair, monkeypatch: pytest.MonkeyPatch
):
    """The lock is released while a signature verifies; a DECLINE resolved in that
    window must not be overwritten by the ACCEPT that was already mid-flight."""
    key, _ = card_keypair
    inner_store, registry = store
    prompt = inner_store.publish(_intent(), mode="paper", broker="stub", ttl_seconds=10)
    sig = key.sign(prompt.canonical.encode("utf-8"))

    real_verify = registry.verify
    racer: dict[str, bool] = {}

    def verify_then_race(card_id: str, canonical: bytes, signature: bytes) -> bool:
        ok = real_verify(card_id, canonical, signature)
        if not racer:
            racer["started"] = True
            racer["decline_ok"] = inner_store.respond(
                prompt.intent_id, decision="DECLINE", card_id="card-001", signature=sig
            )
        return ok

    monkeypatch.setattr(registry, "verify", verify_then_race)
    accept_ok = inner_store.respond(
        prompt.intent_id, decision="ACCEPT", card_id="card-001", signature=sig
    )

    assert racer["decline_ok"] is True
    assert accept_ok is False
    assert inner_store.wait(prompt.intent_id, timeout=0) == "DECLINE"
    pb = inner_store.passbook()
    assert [e.verdict for e in pb] == ["DECLINE"]


def test_autonomous_router_cannot_be_wrapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTONOMOUS_ENABLED", "true")
    inner = Router.build_default(
        mode="autonomous",
        broker=_StubBroker(),
        state_dir=tmp_path,
        daily_max_trades=5,
        daily_max_notional_usd=10_000,
    )
    registry = CardRegistry()
    with pytest.raises(ValueError, match="autonomous"):
        ApprovalRouter(inner, store=InMemoryApprovalStore(registry))


def test_canonical_bytes_stable():
    kw = dict(
        broker="ib", action="Buy", symbol="XIC.TO", shares=12, entry=31.05,
        notional_usd=372.60, account="acct-1", intent_id="abc", nonce="nnn",
    )
    assert canonical_bytes(**kw) == canonical_bytes(**kw)
    # Any field change perturbs the bytes
    assert canonical_bytes(**{**kw, "shares": 13}) != canonical_bytes(**kw)
    # broker swap (ib -> kraken) MUST change the signed bytes
    assert canonical_bytes(**{**kw, "broker": "kraken"}) != canonical_bytes(**kw)
    # Whole-share wire form is frozen: deployed cards have signed this exact layout.
    assert canonical_bytes(**kw) == b"ib|Buy|XIC.TO|12|31.0500|372.60|acct-1|abc|nnn"


def test_canonical_bytes_preserves_fractional_crypto_quantity():
    kw = dict(
        broker="kraken", action="Buy", symbol="BTC/USD", entry=42000.0,
        notional_usd=2100.0, account="k1", intent_id="abc", nonce="nnn",
    )
    assert canonical_bytes(shares=0.05, **kw).split(b"|")[3] == b"0.05"
    assert canonical_bytes(shares=0.00012345, **kw).split(b"|")[3] == b"0.00012345"
    assert canonical_bytes(shares=0.00000005, **kw).split(b"|")[3] == b"0.00000005"
    # Sub-satoshi must neither round up nor collapse to zero on the signed bytes.
    assert canonical_bytes(shares=0.000000005, **kw).split(b"|")[3] == b"0.000000005"
    assert canonical_bytes(shares=0.05, **kw) != canonical_bytes(shares=0.06, **kw)
    assert canonical_bytes(shares=3.0, **kw).split(b"|")[3] == b"3"


def test_publish_carries_broker_and_thesis(paper_router: Router, store):
    inner_store, _ = store
    intent = _intent()
    prompt = inner_store.publish(
        intent, mode="paper", broker="kraken", ttl_seconds=10,
        thesis="reg-shift; momo confirmed by vol",
    )
    assert prompt.broker == "kraken"
    assert "kraken" in prompt.canonical
    assert prompt.thesis.startswith("reg-shift")


# --- broker tag sourcing (2026-09-17) --------------------------------------
# ApprovalRouter published ``broker.name``, but the wire vocabulary is ``broker.venue``:
# IBBroker.name is "interactive-brokers" and IBWebBroker.name is "interactive-brokers-web",
# neither of which the shim's Broker literal accepts. Prompts from those adapters would fail
# response validation the same way "paper" did.

class _VenueBroker(_StubBroker):
    """Shaped like IBBroker: a venue tag that differs from the adapter's name."""
    name = "interactive-brokers"
    venue = "ib"


class _PaperShapedBroker(_StubBroker):
    """Shaped like PaperBroker wrapping a feed."""
    name = "paper"
    venue = "paper"


class _NamedOnlyBroker(_StubBroker):
    """No ``.venue`` at all — the fallback path must still produce something."""
    name = "static-feed"


def _published_broker_tag(router_broker, tmp_path: Path, store) -> str:
    inner_store, _ = store
    inner = Router.build_default(mode="paper", broker=router_broker, state_dir=tmp_path)
    approval = ApprovalRouter(inner, store=inner_store, ttl_seconds=10)
    t = threading.Thread(
        target=lambda: approval.submit(
            _intent(), equity=100_000, existing_risk=0, open_positions=0
        ),
        daemon=True,
    )
    t.start()
    pending: list = []
    for _ in range(200):
        pending = inner_store.pending()
        if pending:
            break
        threading.Event().wait(0.01)
    assert pending, "no prompt published"
    return pending[0].broker


def test_publish_uses_broker_venue_not_name(tmp_path: Path, store):
    tag = _published_broker_tag(_VenueBroker(), tmp_path, store)
    assert tag == "ib"                 # not "interactive-brokers"


def test_publish_falls_back_to_name_without_venue(tmp_path: Path, store):
    tag = _published_broker_tag(_NamedOnlyBroker(), tmp_path, store)
    assert tag == "static-feed"


def test_published_tag_is_accepted_by_the_wire_schema(tmp_path: Path, store):
    """The tag a real paper session publishes must survive the shim's response model."""
    from typing import get_args

    from trading_live_claude.execution.approval_asgi import Broker

    accepted = set(get_args(Broker))
    assert _published_broker_tag(_VenueBroker(), tmp_path, store) in accepted
    assert _published_broker_tag(_PaperShapedBroker(), tmp_path, store) in accepted
