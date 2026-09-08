"""SQLite-backed store/registry tests.

Mirrors the InMemory coverage plus the two things the persistent variant
buys: pubkeys and unresolved prompts survive a restart. The signed-intent
contract and every risk gate are identical to the in-memory path, so we
don't re-test the router-wrapping surface here.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trading_live_claude.brokers.models import OrderAction
from trading_live_claude.execution.approval_sqlite import (
    SqliteApprovalStore,
    SqliteCardRegistry,
)
from trading_live_claude.execution.router import OrderIntent


def _intent(shares: int = 10, entry: float = 100.0) -> OrderIntent:
    return OrderIntent(
        symbol="AAPL",
        action=OrderAction.BUY,
        shares=shares,
        entry=entry,
        stop=entry * 0.96,
        target=entry * 1.08,
        strategy="test",
        risk_dollars=shares * entry * 0.04,
        account_number="PAPER-001",
        symbolId=1,
    )


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "approval.db"


@pytest.fixture
def keypair():
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return key, pem


# --------------------------------------------------------------------------- #
# registry                                                                    #
# --------------------------------------------------------------------------- #

def test_registry_register_verify_revoke_round_trip(db_path, keypair):
    key, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    assert "c1" in reg.card_ids()

    canonical = b"broker|Buy|AAPL|10|100.0000|1000.00|acct|iid|nonce"
    sig = key.sign(canonical)
    assert reg.verify("c1", canonical, sig) is True

    assert reg.revoke("c1") is True
    assert reg.revoke("c1") is False              # second revoke: not-found
    assert reg.verify("c1", canonical, sig) is False   # revoked cards refuse


def test_registry_reregister_after_revoke(db_path, keypair):
    _, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    reg.revoke("c1")
    reg.register("c1", pem)     # same pubkey, still fine — un-revokes the row
    assert "c1" in reg.card_ids()


def test_registry_survives_restart(db_path, keypair):
    _, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c-persist", pem)
    del reg  # close handle
    reg2 = SqliteCardRegistry(db_path)
    assert "c-persist" in reg2.card_ids()


def test_registry_rejects_non_ed25519(db_path):
    reg = SqliteCardRegistry(db_path)
    with pytest.raises(ValueError):
        reg.register("bad", b"-----BEGIN PUBLIC KEY-----\nnot a real key\n-----END PUBLIC KEY-----\n")


# --------------------------------------------------------------------------- #
# store                                                                       #
# --------------------------------------------------------------------------- #

def test_publish_pending_accept_dispatches(db_path, keypair):
    key, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, db_path)

    prompt = store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=5)
    assert store.pending()[0].intent_id == prompt.intent_id

    sig = key.sign(prompt.canonical.encode())
    assert store.respond(prompt.intent_id, decision="ACCEPT", card_id="c1",
                         signature=sig) is True
    # After respond, no longer pending.
    assert store.pending() == []
    # Passbook picks it up.
    pb = store.passbook()
    assert len(pb) == 1
    assert pb[0].verdict == "ACCEPT"
    assert pb[0].card_id == "c1"


def test_wait_returns_accept_verdict(db_path, keypair):
    key, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, db_path)

    prompt = store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=5)

    holder: dict = {}
    t = threading.Thread(target=lambda: holder.__setitem__(
        "v", store.wait(prompt.intent_id)))
    t.start()
    threading.Event().wait(0.05)
    sig = key.sign(prompt.canonical.encode())
    store.respond(prompt.intent_id, decision="ACCEPT", card_id="c1", signature=sig)
    t.join(timeout=2)
    assert holder["v"] == "ACCEPT"


def test_replay_of_signed_response_refused(db_path, keypair):
    key, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, db_path)

    prompt = store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=5)
    sig = key.sign(prompt.canonical.encode())
    assert store.respond(prompt.intent_id, decision="ACCEPT", card_id="c1",
                         signature=sig) is True
    assert store.respond(prompt.intent_id, decision="ACCEPT", card_id="c1",
                         signature=sig) is False


def test_bad_signature_does_not_consume(db_path, keypair):
    _, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, db_path)

    prompt = store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=5)
    imposter = Ed25519PrivateKey.generate()
    bad_sig = imposter.sign(prompt.canonical.encode())
    assert store.respond(prompt.intent_id, decision="ACCEPT", card_id="c1",
                         signature=bad_sig) is False
    # Intent still pending — a genuine card could still respond.
    assert prompt.intent_id in {p.intent_id for p in store.pending()}


def test_expired_prompt_swept_and_journaled(db_path, keypair):
    _, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, db_path)
    store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=0.05)
    import time
    time.sleep(0.15)
    _ = store.pending()   # sweep runs here
    pb = store.passbook()
    assert pb and pb[0].verdict == "EXPIRED"
    assert pb[0].card_id is None


def test_passbook_newest_first_and_paginates(db_path, keypair):
    key, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, db_path)

    ids = []
    for _ in range(5):
        p = store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=5)
        store.respond(p.intent_id, decision="ACCEPT", card_id="c1",
                      signature=key.sign(p.canonical.encode()))
        ids.append(p.intent_id)

    page1 = store.passbook(limit=2, offset=0)
    page2 = store.passbook(limit=2, offset=2)
    assert [e.intent_id for e in page1] == [ids[4], ids[3]]
    assert [e.intent_id for e in page2] == [ids[2], ids[1]]


# --------------------------------------------------------------------------- #
# persistence across restart                                                  #
# --------------------------------------------------------------------------- #

def test_unresolved_prompt_survives_restart(db_path, keypair):
    key, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, db_path)
    prompt = store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=30)
    del store
    del reg

    # Simulate a shim restart.
    reg2 = SqliteCardRegistry(db_path)
    store2 = SqliteApprovalStore(reg2, db_path)
    pending_ids = {p.intent_id for p in store2.pending()}
    assert prompt.intent_id in pending_ids

    # The card can still resolve it against the resurrected shim.
    sig = key.sign(prompt.canonical.encode())
    assert store2.respond(prompt.intent_id, decision="DECLINE", card_id="c1",
                          signature=sig) is True
    pb = store2.passbook()
    assert pb[0].verdict == "DECLINE"


def test_passbook_survives_restart(db_path, keypair):
    key, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, db_path)
    p = store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=5)
    store.respond(p.intent_id, decision="ACCEPT", card_id="c1",
                  signature=key.sign(p.canonical.encode()))
    del store
    del reg

    reg2 = SqliteCardRegistry(db_path)
    store2 = SqliteApprovalStore(reg2, db_path)
    pb = store2.passbook()
    assert len(pb) == 1
    assert pb[0].verdict == "ACCEPT"
    assert pb[0].intent_id == p.intent_id
