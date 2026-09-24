"""Fingerprint continuity: dashboard, device and ledger show the same bytes (scope section 3).

The property under test is *one value in three places*, all derived from the canonical string that
is actually signed. A fingerprint recomputed from the prompt's separate JSON fields would look
identical in the happy case and hide exactly the substitution it exists to catch, so several of
these tests assert the derivation, not just the format.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.test_router import _intent, _StubBroker
from trading_live_claude.audit import Ledger
from trading_live_claude.execution.approval import (
    ApprovalRouter,
    CardRegistry,
    InMemoryApprovalStore,
    canonical_bytes,
    fingerprint,
)
from trading_live_claude.execution.approval_sqlite import (
    SqliteApprovalStore,
    SqliteCardRegistry,
)
from trading_live_claude.execution.router import Router

_SIM_PATH = Path(__file__).resolve().parents[1] / "scripts" / "approval_card_sim.py"
_SPEC = importlib.util.spec_from_file_location("approval_card_sim", _SIM_PATH)
assert _SPEC and _SPEC.loader
sim = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sim)

PWA = Path(__file__).resolve().parents[1] / "pwa" / "index.html"
FP_RE = re.compile(r"^[0-9A-F]{4}\.\.\.[0-9A-F]{4}$")


def _keypair():
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return key, pem


# --- the value itself ------------------------------------------------------------------------

def test_the_shape_matches_the_specified_format() -> None:
    fp = fingerprint(canonical_bytes(
        broker="ib", action="Buy", symbol="AAPL", shares=100, entry=245.50,
        notional_usd=24550.0, account="acct", intent_id="iid", nonce="n1"))
    assert FP_RE.match(fp), fp                     # 7F3A...91C2


def test_it_is_derived_from_the_signed_bytes_not_the_json_fields() -> None:
    """A shim that displays one trade and has the card sign another must produce a mismatch."""
    shown = canonical_bytes(broker="ib", action="Buy", symbol="AAPL", shares=100, entry=245.50,
                            notional_usd=24550.0, account="acct", intent_id="iid", nonce="n1")
    swapped = canonical_bytes(broker="kraken", action="Buy", symbol="AAPL", shares=100,
                              entry=245.50, notional_usd=24550.0, account="acct",
                              intent_id="iid", nonce="n1")
    resized = canonical_bytes(broker="ib", action="Buy", symbol="AAPL", shares=1000, entry=245.50,
                              notional_usd=245500.0, account="acct", intent_id="iid", nonce="n1")
    assert fingerprint(shown) != fingerprint(swapped)      # venue substitution
    assert fingerprint(shown) != fingerprint(resized)      # resize


def test_an_odd_or_tiny_length_is_refused() -> None:
    with pytest.raises(ValueError):
        fingerprint(b"x", chars=7)
    with pytest.raises(ValueError):
        fingerprint(b"x", chars=0)


# --- on the wire -----------------------------------------------------------------------------

def test_a_published_prompt_carries_it(tmp_path: Path) -> None:
    store = InMemoryApprovalStore(CardRegistry())
    prompt = store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=5)
    assert prompt.fingerprint == fingerprint(prompt.canonical.encode("utf-8"))
    assert prompt.to_dict()["fingerprint"] == prompt.fingerprint
    assert FP_RE.match(prompt.fingerprint)


def test_the_passbook_row_carries_the_same_value(tmp_path: Path) -> None:
    """History has to show what was approved, not a fresh hash of today's fields."""
    key, pem = _keypair()
    reg = SqliteCardRegistry(tmp_path / "a.db")
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, tmp_path / "a.db")
    prompt = store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=5)
    store.respond(prompt.intent_id, decision="ACCEPT", card_id="c1",
                  signature=key.sign(prompt.canonical.encode()))
    entry = store.passbook(limit=5)[0]
    assert entry.fingerprint == prompt.fingerprint
    assert entry.to_dict()["fingerprint"] == prompt.fingerprint


def test_the_api_models_expose_it() -> None:
    from trading_live_claude.execution.approval_asgi import PassbookEntryOut, PromptOut

    assert "fingerprint" in PromptOut.model_fields
    assert "fingerprint" in PassbookEntryOut.model_fields


# --- the third place: the ledger --------------------------------------------------------------

def test_prompt_passbook_and_ledger_agree(tmp_path: Path) -> None:
    """One intent, three records, one fingerprint — the actual continuity claim."""
    import threading

    key, pem = _keypair()
    reg = SqliteCardRegistry(tmp_path / "a.db")
    reg.register("c1", pem)
    store = SqliteApprovalStore(reg, tmp_path / "a.db")
    led = Ledger(tmp_path / "ledger", stream="fp", session_id="s1")
    inner = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                 ledger=led)
    router = ApprovalRouter(inner=inner, store=store, ttl_seconds=5)
    captured: dict[str, str] = {}

    def _accept() -> None:
        for _ in range(200):
            pending = store.pending()
            if pending:
                p = pending[0]
                captured["prompt"] = p.fingerprint
                store.respond(p.intent_id, decision="ACCEPT", card_id="c1",
                              signature=key.sign(p.canonical.encode()))
                return
            threading.Event().wait(0.01)

    t = threading.Thread(target=_accept)
    t.start()
    router.submit(_intent(), equity=100_000.0, existing_risk=0.0, open_positions=0)
    t.join(5)

    ledger_fps = {r["payload"]["fingerprint"] for r in led.rows()
                  if isinstance(r.get("payload"), dict) and r["payload"].get("fingerprint")}
    passbook_fp = store.passbook(limit=5)[0].fingerprint
    assert captured["prompt"] == passbook_fp
    assert ledger_fps == {passbook_fp}, ledger_fps


# --- the device side ------------------------------------------------------------------------

def test_the_card_computes_the_same_value_independently() -> None:
    """The sim reimplements it (as firmware must), so a drift between the two is caught here."""
    store = InMemoryApprovalStore(CardRegistry())
    prompt = store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=5)
    assert sim._fingerprint(prompt.canonical) == prompt.fingerprint


def test_the_card_refuses_a_prompt_whose_fingerprint_disagrees() -> None:
    """A served fingerprint that doesn't match the bytes means display and signature have drifted."""
    store = InMemoryApprovalStore(CardRegistry())
    prompt = store.publish(_intent(), mode="paper", broker="ib", ttl_seconds=5)
    served = dict(prompt.to_dict())
    served["fingerprint"] = "DEAD...BEEF"
    assert sim._fingerprint(str(served["canonical"])) != served["fingerprint"]
    # The refusal itself lives in the poll loop; assert the comparison the loop makes.
    assert sim.parse_canonical(served) is not None          # the prompt is otherwise well formed


# --- the dashboard --------------------------------------------------------------------------

def test_the_dashboard_renders_the_served_fingerprint() -> None:
    """Regression guard for the gap found in phase 3: the PWA displayed no fingerprint at all."""
    html = PWA.read_text(encoding="utf-8")
    assert "p.fingerprint" in html          # pending prompts
    assert "e.fingerprint" in html          # passbook history
    assert "HASH" in html
    # It must render what the server served for THAT record, never recompute from the fields.
    assert "sha256" not in html.lower()
