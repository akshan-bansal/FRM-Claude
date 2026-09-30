"""Shim basket endpoints: analyse, propose, then only a card signature makes it an approval."""
from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from trading_live_claude.execution.approval import CardRegistry, InMemoryApprovalStore
from trading_live_claude.execution.approval_asgi import create_app
from trading_live_claude.execution.approval_sqlite import SqliteCardRegistry
from trading_live_claude.execution.basket import append_row, pending_proposals, sign_proposal

H = {"Authorization": "Bearer t"}


@pytest.fixture
def env(tmp_path: Path):
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    with SqliteCardRegistry(tmp_path / "approval.db") as reg:
        reg.register("card-t", pem)
    r = CardRegistry()
    app = create_app(InMemoryApprovalStore(r), r, auth_token="t", state_dir=tmp_path)
    return TestClient(app), key, tmp_path


def test_analyze_needs_the_token(env) -> None:
    c, _, _ = env
    assert c.post("/v1/basket/analyze", json={"venue": "qt", "symbols": ["XIC.TO"]}).status_code == 401


def test_analyze_reports_evidence_and_gaps(env) -> None:
    c, _, _ = env
    rep = c.post("/v1/basket/analyze", headers=H, json={"venue": "qt", "symbols": ["XIC.TO", "NOPE"]}).json()
    assert rep["scope"]["symbols"] == 2 and rep["scope"]["with_stats"] == 0
    assert all(r["stats"] is None for r in rep["rows"])


def test_propose_is_not_an_approval_until_the_card_signs(env) -> None:
    c, key, tmp = env
    prop = c.post("/v1/basket/propose", headers=H, json={"venue": "qt", "symbols": ["XIC.TO", "VOO"]}).json()
    assert c.get("/v1/basket", headers=H).json()["venues"]["qt"] is None       # proposed, not approved
    assert [p["id"] for p in c.get("/v1/basket", headers=H).json()["proposals"]] == [prop["id"]]
    full = pending_proposals(tmp / "basket_proposals.jsonl")[0]
    assert full["fingerprint"] == prop["fingerprint"]
    append_row(sign_proposal(full, card_id="card-t", sign=key.sign), tmp / "baskets.jsonl")
    got = c.get("/v1/basket", headers=H).json()
    assert got["venues"]["qt"]["symbols"] == ["VOO", "XIC.TO"] and got["proposals"] == []


def test_ib_equities_are_refused_at_propose(env) -> None:
    c, _, _ = env
    r = c.post("/v1/basket/propose", headers=H, json={"venue": "ib", "symbols": ["AAPL", "GC"]})
    assert r.status_code == 422 and "AAPL" in r.json()["detail"]


def test_stale_proposal_cannot_be_signed(env) -> None:
    c, key, tmp = env
    c.post("/v1/basket/propose", headers=H, json={"venue": "qt", "symbols": ["XIC.TO"]})
    full = pending_proposals(tmp / "basket_proposals.jsonl")[0]
    full["issued_at"] = "2020-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="older than 30 minutes"):
        sign_proposal(full, card_id="card-t", sign=key.sign)
