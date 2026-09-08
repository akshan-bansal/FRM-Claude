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


# --------------------------------------------------------------------------- #
# passbook                                                                    #
# --------------------------------------------------------------------------- #

def _publish_and_respond(shim, key, card_id, decision):
    intent_body = {
        "symbol": "AAA", "action": "Buy", "shares": 1, "entry": 10.0,
        "stop": 9.0, "target": 11.0, "strategy": "t",
        "risk_dollars": 0.5, "account_number": "p1",
        "broker": "ib", "ttl_seconds": 5,
    }
    _, prompt = _post(f"{shim['url']}/intents", intent_body)
    sig = key.sign(prompt["canonical"].encode())
    _post(
        f"{shim['url']}/intents/{prompt['intent_id']}/response",
        {"decision": decision, "card_id": card_id,
         "signature": base64.b64encode(sig).decode()},
    )
    return prompt["intent_id"]


def test_passbook_returns_resolved_prompts_newest_first(shim):
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    _post(f"{shim['url']}/card/register",
          {"card_id": "cp", "pubkey_pem": pem.decode()})

    id_a = _publish_and_respond(shim, key, "cp", "ACCEPT")
    id_b = _publish_and_respond(shim, key, "cp", "DECLINE")

    status, body = _get(f"{shim['url']}/passbook")
    assert status == 200
    ids = [e["intent_id"] for e in body["entries"]]
    verdicts = [e["verdict"] for e in body["entries"]]
    assert ids == [id_b, id_a]                 # newest first
    assert verdicts == ["DECLINE", "ACCEPT"]
    # signer card_id round-trips
    for e in body["entries"]:
        assert e["card_id"] == "cp"


def test_passbook_pagination(shim):
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    _post(f"{shim['url']}/card/register",
          {"card_id": "cp", "pubkey_pem": pem.decode()})
    ids = [_publish_and_respond(shim, key, "cp", "ACCEPT") for _ in range(5)]

    status, body = _get(f"{shim['url']}/passbook?limit=2&offset=0")
    assert status == 200
    assert len(body["entries"]) == 2
    assert [e["intent_id"] for e in body["entries"]] == [ids[4], ids[3]]

    status, body = _get(f"{shim['url']}/passbook?limit=2&offset=2")
    assert [e["intent_id"] for e in body["entries"]] == [ids[2], ids[1]]


def test_passbook_rejects_bad_params(shim):
    status, _ = _get(f"{shim['url']}/passbook?limit=abc")
    assert status == 400


def test_passbook_requires_auth(shim_auth):
    status, _ = _get(f"{shim_auth['url']}/passbook")
    assert status == 401
    status, body = _get_auth(f"{shim_auth['url']}/passbook", shim_auth["token"])
    assert status == 200
    assert body["entries"] == []


# --------------------------------------------------------------------------- #
# OpenAPI                                                                     #
# --------------------------------------------------------------------------- #

_VALID_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def _fetch_spec(url):
    req = urllib.request.Request(f"{url}/openapi.json")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.headers, json.loads(r.read().decode())


def test_openapi_is_public(shim_auth):
    # No bearer at all.
    status, headers, spec = _fetch_spec(shim_auth["url"])
    assert status == 200
    assert headers.get("ETag")
    assert spec["openapi"].startswith("3.1")


def test_openapi_shape_looks_like_openapi(shim):
    status, _, spec = _fetch_spec(shim["url"])
    assert status == 200
    assert "info" in spec and "title" in spec["info"] and "version" in spec["info"]
    assert "paths" in spec and spec["paths"]
    assert "components" in spec and "schemas" in spec["components"]
    assert "BearerAuth" in spec["components"]["securitySchemes"]
    # Every path uses a valid HTTP method
    for path, ops in spec["paths"].items():
        assert path.startswith("/"), path
        for method in ops:
            assert method in _VALID_METHODS, (path, method)
    # Every $ref points at a component that exists
    ref_targets = _walk_refs(spec)
    for ref in ref_targets:
        assert ref.startswith("#/components/schemas/"), ref
        name = ref.split("/")[-1]
        assert name in spec["components"]["schemas"], name


def _walk_refs(node, acc=None):
    if acc is None:
        acc = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                acc.append(v)
            else:
                _walk_refs(v, acc)
    elif isinstance(node, list):
        for v in node:
            _walk_refs(v, acc)
    return acc


def test_openapi_covers_every_live_route(shim):
    # Drift guard: every route the shim actually serves must appear in the spec.
    _, _, spec = _fetch_spec(shim["url"])

    live_routes = {
        ("GET",    "/healthz"),
        ("GET",    "/openapi.json"),
        ("POST",   "/card/register"),
        ("DELETE", "/card/{card_id}"),
        ("POST",   "/intents"),
        ("GET",    "/intents/pending"),
        ("GET",    "/intents/{intent_id}"),
        ("POST",   "/intents/{intent_id}/response"),
        ("GET",    "/intel/{ref}"),
        ("GET",    "/passbook"),
    }
    spec_routes = {(m.upper(), p) for p, ops in spec["paths"].items() for m in ops}
    missing = live_routes - spec_routes
    assert not missing, f"spec missing routes: {missing}"


def test_openapi_etag_304(shim):
    _, headers, _ = _fetch_spec(shim["url"])
    etag = headers.get("ETag")
    assert etag
    req = urllib.request.Request(f"{shim['url']}/openapi.json",
                                 headers={"If-None-Match": etag})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 304


def test_passbook_captures_expired(shim):
    # Publish an intent that will expire before a response comes.
    intent_body = {
        "symbol": "AAA", "action": "Buy", "shares": 1, "entry": 10.0,
        "stop": 9.0, "target": 11.0, "strategy": "t",
        "risk_dollars": 0.5, "account_number": "p1",
        "broker": "ib", "ttl_seconds": 0.05,
    }
    _post(f"{shim['url']}/intents", intent_body)
    import time
    time.sleep(0.2)
    # A pending() call sweeps expired; passbook() then returns the record.
    _get(f"{shim['url']}/intents/pending")
    status, body = _get(f"{shim['url']}/passbook")
    assert status == 200
    assert body["entries"]
    assert body["entries"][0]["verdict"] == "EXPIRED"
    assert body["entries"][0]["card_id"] is None


def test_publish_rejects_unknown_broker(shim):
    status, body = _post(f"{shim['url']}/intents", {
        "symbol": "X", "action": "Buy", "shares": 1, "entry": 1.0,
        "stop": 0.9, "target": 1.1, "strategy": "t", "risk_dollars": 0.1,
        "account_number": "a", "broker": "robinhood",
    })
    assert status == 400
    assert "robinhood" in body["error"]
