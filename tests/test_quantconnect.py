from __future__ import annotations

import base64
import hashlib

import httpx
import pytest
import respx

from trading_live_claude.integrations.quantconnect import (
    QC_API_BASE,
    QuantConnectClient,
    QuantConnectError,
)

USER_ID = "528370"
TOKEN = "test-token-abc"


def _client() -> QuantConnectClient:
    return QuantConnectClient(USER_ID, TOKEN)


def test_missing_credentials_rejected() -> None:
    with pytest.raises(QuantConnectError):
        QuantConnectClient("", TOKEN)
    with pytest.raises(QuantConnectError):
        QuantConnectClient(USER_ID, "")


def test_auth_header_matches_qc_scheme() -> None:
    ts = 1_700_000_000
    headers = _client()._auth_headers(timestamp=ts)

    assert headers["Timestamp"] == str(ts)
    expected_hash = hashlib.sha256(f"{TOKEN}:{ts}".encode()).hexdigest()
    expected = base64.b64encode(f"{USER_ID}:{expected_hash}".encode()).decode()
    assert headers["Authorization"] == f"Basic {expected}"


@respx.mock
def test_authenticate_success() -> None:
    route = respx.post(f"{QC_API_BASE}/authenticate").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    assert _client().authenticate() is True
    assert route.called
    # Auth headers actually went out on the wire.
    sent = route.calls.last.request
    assert sent.headers["Authorization"].startswith("Basic ")
    assert "Timestamp" in sent.headers


@respx.mock
def test_success_false_raises_with_errors() -> None:
    respx.post(f"{QC_API_BASE}/authenticate").mock(
        return_value=httpx.Response(200, json={"success": False, "errors": ["bad creds"]})
    )
    with pytest.raises(QuantConnectError, match="bad creds"):
        _client().authenticate()


@respx.mock
def test_http_error_wrapped() -> None:
    respx.post(f"{QC_API_BASE}/authenticate").mock(return_value=httpx.Response(500))
    with pytest.raises(QuantConnectError, match="failed"):
        _client().authenticate()


@respx.mock
def test_create_project_and_backtest_flow() -> None:
    respx.post(f"{QC_API_BASE}/projects/create").mock(
        return_value=httpx.Response(200, json={"success": True, "projects": [{"projectId": 42}]})
    )
    respx.post(f"{QC_API_BASE}/compile/create").mock(
        return_value=httpx.Response(200, json={"success": True, "compileId": "c1"})
    )
    respx.post(f"{QC_API_BASE}/backtests/create").mock(
        return_value=httpx.Response(200, json={"success": True, "backtest": {"backtestId": "b1"}})
    )

    c = _client()
    proj = c.create_project("demo")
    assert proj["projects"] == [{"projectId": 42}]
    compiled = c.compile_project(42)
    assert compiled["compileId"] == "c1"
    bt = c.create_backtest(42, "c1", "run-1")
    assert bt["backtest"] == {"backtestId": "b1"}


@respx.mock
def test_wait_for_backtest_completes() -> None:
    respx.post(f"{QC_API_BASE}/backtests/read").mock(
        side_effect=[
            httpx.Response(200, json={"success": True, "backtest": {"completed": False}}),
            httpx.Response(200, json={"success": True, "backtest": {"completed": True, "statistics": {}}}),
        ]
    )
    data = _client().wait_for_backtest(42, "b1", poll_seconds=0.0, timeout_seconds=5.0)
    assert data["backtest"]["completed"] is True


@respx.mock
def test_put_file_creates_when_absent() -> None:
    route = respx.post(f"{QC_API_BASE}/files/create").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    _client().put_file(42, "main.py", "x = 1")
    assert route.called


@respx.mock
def test_put_file_falls_back_to_update_when_exists() -> None:
    respx.post(f"{QC_API_BASE}/files/create").mock(
        return_value=httpx.Response(200, json={"success": False, "errors": ["File already exist"]})
    )
    upd = respx.post(f"{QC_API_BASE}/files/update").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    _client().put_file(42, "main.py", "x = 2")
    assert upd.called  # the create failed → update was tried


@respx.mock
def test_wait_for_compile_success() -> None:
    respx.post(f"{QC_API_BASE}/compile/read").mock(
        side_effect=[
            httpx.Response(200, json={"success": True, "state": "InQueue"}),
            httpx.Response(200, json={"success": True, "state": "BuildSuccess"}),
        ]
    )
    data = _client().wait_for_compile(42, "c1", poll_seconds=0.0, timeout_seconds=5.0)
    assert data["state"] == "BuildSuccess"


@respx.mock
def test_wait_for_compile_build_error_raises() -> None:
    respx.post(f"{QC_API_BASE}/compile/read").mock(
        return_value=httpx.Response(200, json={"success": True, "state": "BuildError", "logs": "syntax"})
    )
    with pytest.raises(QuantConnectError, match="Compile failed"):
        _client().wait_for_compile(42, "c1", poll_seconds=0.0, timeout_seconds=5.0)


@respx.mock
def test_wait_for_backtest_error_raises() -> None:
    respx.post(f"{QC_API_BASE}/backtests/read").mock(
        return_value=httpx.Response(200, json={"success": True, "backtest": {"error": "boom"}})
    )
    with pytest.raises(QuantConnectError, match="boom"):
        _client().wait_for_backtest(42, "b1", poll_seconds=0.0, timeout_seconds=5.0)
