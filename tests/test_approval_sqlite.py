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


def test_response_losing_race_reports_false(db_path, keypair, monkeypatch):
    """The CAS update protects the verdict; the losing responder must be told so."""
    key, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, db_path)
    prompt = store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=5)
    sig = key.sign(prompt.canonical.encode())

    real_verify = reg.verify
    racer: dict = {}

    def verify_then_race(card_id, canonical, signature):
        ok = real_verify(card_id, canonical, signature)
        if not racer:
            racer["started"] = True
            racer["decline_ok"] = store.respond(
                prompt.intent_id, decision="DECLINE", card_id="c1", signature=sig
            )
        return ok

    monkeypatch.setattr(reg, "verify", verify_then_race)
    accept_ok = store.respond(prompt.intent_id, decision="ACCEPT", card_id="c1",
                              signature=sig)

    assert racer["decline_ok"] is True
    assert accept_ok is False
    assert [e.verdict for e in store.passbook()] == ["DECLINE"]


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


# --- fractional quantity fidelity (2026-09-17) -----------------------------
# The ``shares`` column was INTEGER and both decoders cast with int(), so a crypto-sleeve size
# like 4.18347861 PAXG came back as 4 — the passbook disagreed with the fill, and a prompt
# rehydrated after a restart would have DISPLAYED a size the card never signed. Found by the
# end-to-end card run on 2026-09-17 (passbook said shares=4.0 for a 4.18347861 fill).

def _crypto_intent(shares: float) -> OrderIntent:
    return OrderIntent(
        symbol="PAXG/USD", action=OrderAction.BUY, shares=shares, entry=4356.40,
        stop=4100.0, target=4600.0, strategy="ts_momentum",
        risk_dollars=508.69, account_number="PAPER-001", symbolId=1,
    )


@pytest.mark.parametrize("shares", [4.18347861, 0.00012345, 0.000000005, 3.0])
def test_pending_prompt_round_trips_fractional_shares(db_path, keypair, shares):
    _, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, db_path)

    published = store.publish(_crypto_intent(shares), mode="paper", broker="paper",
                              ttl_seconds=30)
    assert published.shares == shares
    # Read back through the row decoder — this is the path a restarted shim serves from.
    assert store.pending()[0].shares == shares


def test_rehydrated_canonical_still_verifies(db_path, keypair):
    """The bytes a card signs must survive persistence, or a restart invalidates the tap."""
    key, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, db_path)
    prompt = store.publish(_crypto_intent(4.18347861), mode="paper", broker="paper",
                           ttl_seconds=30)

    reloaded = SqliteApprovalStore(SqliteCardRegistry(db_path), db_path).pending()[0]
    assert reloaded.canonical == prompt.canonical
    assert reloaded.canonical.split("|")[3] == "4.18347861"
    sig = key.sign(reloaded.canonical.encode())
    assert store.respond(reloaded.intent_id, decision="ACCEPT", card_id="c1",
                         signature=sig) is True


def test_passbook_keeps_the_fractional_size(db_path, keypair):
    key, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, db_path)
    prompt = store.publish(_crypto_intent(4.18347861), mode="paper", broker="paper",
                           ttl_seconds=30)
    store.respond(prompt.intent_id, decision="ACCEPT", card_id="c1",
                  signature=key.sign(prompt.canonical.encode()))
    assert store.passbook()[0].shares == 4.18347861


# --------------------------------------------------------------------------- #
# audit evidence (2026-09-24): the signature is kept, not just checked        #
# --------------------------------------------------------------------------- #

def test_signature_is_persisted_and_reverifies_offline(db_path, keypair):
    """Before this, `verify()` checked the signature and dropped it — the only record that an
    approval was genuine was the router's own word. Now it re-verifies from the DB alone."""
    from trading_live_claude.execution.approval import verify_audit_record

    key, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, db_path)
    prompt = store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=5)
    sig = key.sign(prompt.canonical.encode())
    assert store.respond(prompt.intent_id, decision="ACCEPT", card_id="c1", signature=sig) is True

    rec = store.audit_record(prompt.intent_id)
    assert rec is not None
    assert rec["signature"] and rec["signature_alg"] == "ed25519"
    assert rec["canonical"] == prompt.canonical
    assert rec["signer_card_id"] == "c1"
    assert verify_audit_record(rec) == (True, "ok")


def test_audit_record_survives_a_restart_and_a_card_revocation(db_path, keypair):
    """Revoking a card today must not erase the evidence that it was valid when it signed."""
    from trading_live_claude.execution.approval import verify_audit_record

    key, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, db_path)
    prompt = store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=5)
    store.respond(prompt.intent_id, decision="ACCEPT", card_id="c1",
                  signature=key.sign(prompt.canonical.encode()))
    reg.revoke("c1")
    del store, reg

    reg2 = SqliteCardRegistry(db_path)
    store2 = SqliteApprovalStore(reg2, db_path)
    rec = store2.audit_record(prompt.intent_id)
    assert rec is not None and rec["card_revoked_at"]
    assert verify_audit_record(rec) == (True, "ok")


def test_tampered_canonical_bytes_fail_verification(db_path, keypair):
    """The point of storing the signature: an edited audit row is detectable."""
    from trading_live_claude.execution.approval import verify_audit_record

    key, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, db_path)
    prompt = store.publish(_intent(shares=10), mode="paper", broker="ib", ttl_seconds=5)
    store.respond(prompt.intent_id, decision="ACCEPT", card_id="c1",
                  signature=key.sign(prompt.canonical.encode()))
    rec = dict(store.audit_record(prompt.intent_id) or {})
    rec["canonical"] = str(rec["canonical"]).replace("|10|", "|1000|")   # resize after the fact
    ok, reason = verify_audit_record(rec)
    assert ok is False and "does not verify" in reason


def test_missing_or_unsigned_evidence_never_reads_as_verified(db_path, keypair):
    """"No signature on file" must not be reported as a pass — that was the old silent state."""
    from trading_live_claude.execution.approval import verify_audit_record

    key, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, db_path)

    expired = store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=0.01)
    threading.Event().wait(0.05)
    store.pending()                                        # sweeps it to EXPIRED
    ok, reason = verify_audit_record(store.audit_record(expired.intent_id) or {})
    assert ok is False and "nothing signed it" in reason

    accepted = store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=5)
    store.respond(accepted.intent_id, decision="ACCEPT", card_id="c1",
                  signature=key.sign(accepted.canonical.encode()))
    legacy = dict(store.audit_record(accepted.intent_id) or {})
    legacy["signature"] = None                             # a pre-2026-09-24 row
    ok, reason = verify_audit_record(legacy)
    assert ok is False and "predates" in reason

    assert store.audit_record("no-such-intent") is None


def test_an_existing_database_gains_the_signature_columns(db_path, keypair):
    """state/approval.db already exists; CREATE TABLE IF NOT EXISTS would never add a column."""
    import sqlite3

    # The pre-2026-09-24 schema, spelled out: no signature / signature_alg columns.
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE cards (card_id TEXT PRIMARY KEY, pubkey_pem TEXT NOT NULL,
                            created_at TEXT NOT NULL, revoked_at TEXT);
        CREATE TABLE intents (
            intent_id TEXT PRIMARY KEY, issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            resolved_at TEXT, verdict TEXT, consumed INTEGER NOT NULL DEFAULT 0,
            broker TEXT NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL,
            shares REAL NOT NULL, entry REAL NOT NULL, stop REAL NOT NULL, target REAL,
            notional_usd REAL NOT NULL, risk_dollars REAL NOT NULL, strategy TEXT NOT NULL,
            account TEXT NOT NULL, mode TEXT NOT NULL, thesis TEXT NOT NULL DEFAULT '',
            intel_ref TEXT NOT NULL DEFAULT '', nonce TEXT NOT NULL, canonical TEXT NOT NULL,
            signer_card_id TEXT);
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(intents)")}
    conn.close()
    assert "signature" not in cols                         # the old shape, as deployed

    key, pem = keypair
    reg = SqliteCardRegistry(db_path)                      # opening runs the migration
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, db_path)
    prompt = store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=5)
    store.respond(prompt.intent_id, decision="ACCEPT", card_id="c1",
                  signature=key.sign(prompt.canonical.encode()))
    rec = store.audit_record(prompt.intent_id)
    assert rec is not None and rec["signature"]


def test_fingerprint_is_stable_and_bound_to_the_exact_bytes():
    """The dashboard/device/ledger comparison value. Same bytes -> same fingerprint."""
    from trading_live_claude.execution.approval import canonical_bytes, fingerprint

    args = dict(broker="ib", action="Buy", symbol="AAPL", shares=100, entry=245.50,
                notional_usd=24550.0, account="acct", intent_id="iid", nonce="n1")
    fp = fingerprint(canonical_bytes(**args))
    assert fp == fingerprint(canonical_bytes(**args))       # deterministic
    assert "..." in fp and len(fp.replace("...", "")) == 8
    resized = fingerprint(canonical_bytes(**{**args, "shares": 1000}))
    assert resized != fp                                   # a resize changes it


# --- connection lifecycle (2026-09-25) -------------------------------------- #

def test_close_is_idempotent_and_releases_the_connection(db_path, keypair):
    """45 ResourceWarnings a run came from instances reclaimed with their connection open."""
    _, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    reg.close()
    reg.close()                                  # twice must not raise
    assert reg._conn is None


def test_the_registry_and_store_work_as_context_managers(db_path, keypair):
    key, pem = keypair
    with SqliteCardRegistry(db_path) as reg:
        reg.register("c1", pem)
        with SqliteApprovalStore(reg, db_path) as store:
            prompt = store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=5)
            assert store.respond(prompt.intent_id, decision="ACCEPT", card_id="c1",
                                 signature=key.sign(prompt.canonical.encode())) is True
        assert store._conn is None
    assert reg._conn is None
    # The data is still on disk: closing a connection is not losing the record.
    with SqliteCardRegistry(db_path) as reg2, SqliteApprovalStore(reg2, db_path) as store2:
        assert store2.audit_record(prompt.intent_id) is not None


def test_a_closed_instance_does_not_leak_the_file_handle(db_path, keypair, tmp_path):
    """On Windows an open handle also keeps the .db locked against a later cleanup."""
    _, pem = keypair
    reg = SqliteCardRegistry(db_path)
    reg.register("c1", pem)
    reg.close()
    db_path.unlink()                             # would raise PermissionError if still held
    assert not db_path.exists()
