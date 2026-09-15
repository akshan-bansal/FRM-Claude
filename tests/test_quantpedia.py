from __future__ import annotations

import base64

import httpx
import pytest
import respx

from trading_live_claude.integrations.quantpedia import (
    QUANTPEDIA_BASE,
    QuantpediaClient,
    QuantpediaError,
    matches,
)


def _client() -> QuantpediaClient:
    return QuantpediaClient("me", "key123")


@respx.mock
def test_list_uses_basic_auth_and_returns_rows() -> None:
    route = respx.get(f"{QUANTPEDIA_BASE}/strategy").mock(return_value=httpx.Response(
        200, json=[{"id": "7", "name": "Term Structure Effect in Commodities", "path": "/x"}]))
    with _client() as qp:
        assert qp.list_strategies()[0]["id"] == "7"
    sent = route.calls[0].request.headers["Authorization"]
    assert sent == "Basic " + base64.b64encode(b"me:key123").decode()


@respx.mock
def test_performance_is_a_sorted_date_indexed_series_requested_as_records() -> None:
    route = respx.get(f"{QUANTPEDIA_BASE}/strategy/7/performance").mock(return_value=httpx.Response(
        200, json=[{"date": "2020-01-02", "performance": 1.02}, {"date": "2020-01-01", "performance": 1.0}]))
    with _client() as qp:
        s = qp.performance("7")
    assert route.calls[0].request.url.params["orient"] == "records"
    assert list(s.index.strftime("%Y-%m-%d")) == ["2020-01-01", "2020-01-02"]
    assert s.iloc[-1] == pytest.approx(1.02)


@respx.mock
@pytest.mark.parametrize(("status", "msg"), [(401, "invalid credentials"), (403, "access rights"),
                                             (404, "not found")])
def test_http_errors_are_explained(status: int, msg: str) -> None:
    respx.get(f"{QUANTPEDIA_BASE}/strategy/9").mock(return_value=httpx.Response(status))
    with _client() as qp, pytest.raises(QuantpediaError, match=msg):
        qp.strategy("9")


@respx.mock
def test_source_code_and_paper() -> None:
    respx.get(f"{QUANTPEDIA_BASE}/strategy/7/source-code").mock(return_value=httpx.Response(
        200, json=[{"file_name": "main.py", "modified_at": "2026-01-01T00:00:00Z", "content": "class A: pass"}]))
    respx.get(f"{QUANTPEDIA_BASE}/paper/f1").mock(return_value=httpx.Response(200, content=b"%PDF-1.7"))
    with _client() as qp:
        assert qp.source_code("7")[0]["file_name"] == "main.py"
        assert qp.paper_pdf("f1").startswith(b"%PDF")


def test_missing_credentials_refuse_and_matching_spans_metadata() -> None:
    with pytest.raises(QuantpediaError, match="QUANTPEDIA_USERNAME"):
        QuantpediaClient("", "")
    detail = {"name": "Term Structure Effect", "description": "roll yield", "keywords": ["carry"],
              "instruments": ["futures"], "marketFactors": ["Commodities"]}
    assert matches(detail, ["commodities", "futures", "carry"])
    assert not matches(detail, ["equities"])
