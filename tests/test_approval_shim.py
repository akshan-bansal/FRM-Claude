"""HTTP-level tests for the approval shim — pending, response, /intel/{ref}."""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trading_live_claude.execution.approval import (
    CardRegistry,
    InMemoryApprovalStore,
)
from trading_live_claude.execution.approval_server import start_shim_thread


def _find_free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads((e.read() or b"{}").decode())


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads((e.read() or b"{}").decode())


@pytest.fixture
def shim(tmp_path, monkeypatch):
    from trading_live_claude.intel import vs_engine
    writeup_dir = tmp_path / "writeups"
    writeup_dir.mkdir()
    monkeypatch.setattr(vs_engine, "DEFAULT_WRITEUP_DIR", writeup_dir)
    import trading_live_claude.execution.approval_server as srv_mod
    monkeypatch.setattr(srv_mod, "DEFAULT_WRITEUP_DIR", writeup_dir)

    registry = CardRegistry()
    store = InMemoryApprovalStore(registry)
    port = _find_free_port()
    start_shim_thread(store, registry, "127.0.0.1", port)
    import time
    for _ in range(50):
        try:
            _get(f"http://127.0.0.1:{port}/healthz")
            break
        except urllib.error.URLError:
            time.sleep(0.02)
    yield {"url": f"http://127.0.0.1:{port}", "store": store, "registry": registry,
           "writeup_dir": writeup_dir}


def test_intel_endpoint_returns_writeup(shim):
    ref = "vs_test_abc"
    payload = {"intel_ref": ref, "thesis": "unit test", "warnings": []}
    (shim["writeup_dir"] / f"{ref}.json").write_text(json.dumps(payload))
    status, body = _get(f"{shim['url']}/intel/{ref}")
    assert status == 200
    assert body["thesis"] == "unit test"


def test_intel_endpoint_404_for_unknown(shim):
    status, _ = _get(f"{shim['url']}/intel/vs_nope")
    assert status == 404


@pytest.mark.parametrize("bad", ["..", "../etc", "a/b", "a\\b", ""])
def test_intel_endpoint_rejects_path_traversal(shim, bad):
    import urllib.parse
    url = f"{shim['url']}/intel/{urllib.parse.quote(bad, safe='')}"
    status, _ = _get(url)
    assert status in (400, 404)


def test_publish_and_respond_flow(shim):
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    status, _ = _post(f"{shim['url']}/card/register",
                      {"card_id": "c1", "pubkey_pem": pem.decode()})
    assert status == 201

    intent_body = {
        "symbol": "XIC.TO", "action": "Buy", "shares": 12, "entry": 31.05,
        "stop": 30.40, "target": 32.10, "strategy": "test",
        "risk_dollars": 7.80, "account_number": "paper-001",
        "broker": "ib", "ttl_seconds": 5,
    }
    status, prompt = _post(f"{shim['url']}/intents", intent_body)
    assert status == 201
    assert prompt["broker"] == "ib"
    assert "canonical" in prompt

    sig = key.sign(prompt["canonical"].encode())
    status, resp = _post(
        f"{shim['url']}/intents/{prompt['intent_id']}/response",
        {"decision": "ACCEPT", "card_id": "c1",
         "signature": base64.b64encode(sig).decode()},
    )
    assert status == 200
    assert resp["accepted"] is True


def test_revoke_card(shim):
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    status, _ = _post(f"{shim['url']}/card/register",
                      {"card_id": "cX", "pubkey_pem": pem.decode()})
    assert status == 201
    assert "cX" in shim["registry"].card_ids()

    req = urllib.request.Request(f"{shim['url']}/card/cX", method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = json.loads((e.read() or b"{}").decode())
    assert body["revoked"] is True
    assert "cX" not in shim["registry"].card_ids()


@pytest.fixture
def shim_auth(tmp_path, monkeypatch):
    from trading_live_claude.intel import vs_engine
    writeup_dir = tmp_path / "writeups"
    writeup_dir.mkdir()
    monkeypatch.setattr(vs_engine, "DEFAULT_WRITEUP_DIR", writeup_dir)
    import trading_live_claude.execution.approval_server as srv_mod
    monkeypatch.setattr(srv_mod, "DEFAULT_WRITEUP_DIR", writeup_dir)

    registry = CardRegistry()
    store = InMemoryApprovalStore(registry)
    port = _find_free_port()
    token = "s3cret-token-x"
    from trading_live_claude.execution.approval_server import start_shim_thread
    start_shim_thread(store, registry, "127.0.0.1", port, auth_token=token)
    import time
    for _ in range(50):
        try:
            _get(f"http://127.0.0.1:{port}/healthz")
            break
        except urllib.error.URLError:
            time.sleep(0.02)
    yield {"url": f"http://127.0.0.1:{port}", "token": token,
           "store": store, "registry": registry, "writeup_dir": writeup_dir}


def _post_auth(url, body, token):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {token}"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads((e.read() or b"{}").decode())


def _get_auth(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads((e.read() or b"{}").decode())


def test_auth_healthz_public_even_when_required(shim_auth):
    status, body = _get(f"{shim_auth['url']}/healthz")
    assert status == 200
    assert body["ok"] is True


def test_auth_rejects_missing_bearer(shim_auth):
    status, _ = _get(f"{shim_auth['url']}/intents/pending")
    assert status == 401


def test_auth_rejects_wrong_bearer(shim_auth):
    status, _ = _get_auth(f"{shim_auth['url']}/intents/pending", "not-the-token")
    assert status == 401


def test_auth_accepts_correct_bearer(shim_auth):
    status, body = _get_auth(f"{shim_auth['url']}/intents/pending", shim_auth["token"])
    assert status == 200
    assert body["prompts"] == []


def test_publish_rejects_unknown_broker(shim):
    status, body = _post(f"{shim['url']}/intents", {
        "symbol": "X", "action": "Buy", "shares": 1, "entry": 1.0,
        "stop": 0.9, "target": 1.1, "strategy": "t", "risk_dollars": 0.1,
        "account_number": "a", "broker": "robinhood",
    })
    assert status == 400
    assert "robinhood" in body["error"]
