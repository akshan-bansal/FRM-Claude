from __future__ import annotations

import httpx
import respx

from trading_live_claude.integrations.qc_library import (
    categorize,
    list_library,
    pull_algorithm,
)
from trading_live_claude.integrations.quantconnect import QC_API_BASE, QuantConnectClient


def _client() -> QuantConnectClient:
    return QuantConnectClient("528370", "tok")


def test_categorize_by_keyword() -> None:
    assert categorize("RSI Mean Reversion") == "mean_reversion"
    assert categorize("Momentum Breakout System") == "momentum"
    assert categorize("ATR Volatility Channel") == "volatility"
    assert categorize("Turn of Month Seasonal") == "seasonality"
    assert categorize("My Random Project") == "uncategorized"


@respx.mock
def test_list_library_normalizes_projects() -> None:
    respx.post(f"{QC_API_BASE}/projects/read").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "projects": [
                    {"projectId": 1, "name": "Bollinger Reversion", "language": "Py", "modified": "2026-01-01"},
                    {"projectId": 2, "name": "Donchian Breakout", "language": "Py", "modified": "2026-02-01"},
                ],
            },
        )
    )
    lib = list_library(_client())
    assert {s.project_id for s in lib} == {1, 2}
    by_id = {s.project_id: s for s in lib}
    assert by_id[1].category == "mean_reversion"
    assert by_id[2].category == "momentum"


@respx.mock
def test_pull_algorithm_returns_sources() -> None:
    respx.post(f"{QC_API_BASE}/files/read").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "files": [
                    {"name": "main.py", "content": "class A(QCAlgorithm): pass"},
                    {"name": "helper.py", "content": "X = 1"},
                ],
            },
        )
    )
    sources = pull_algorithm(_client(), 42)
    assert sources["main.py"].startswith("class A")
    assert sources["helper.py"] == "X = 1"


@respx.mock
def test_read_file_extracts_single_content() -> None:
    respx.post(f"{QC_API_BASE}/files/read").mock(
        return_value=httpx.Response(
            200, json={"success": True, "files": [{"name": "main.py", "content": "hello"}]}
        )
    )
    assert _client().read_file(42, "main.py") == "hello"


@respx.mock
def test_list_projects_handles_empty() -> None:
    respx.post(f"{QC_API_BASE}/projects/read").mock(
        return_value=httpx.Response(200, json={"success": True, "projects": []})
    )
    assert list_library(_client()) == []
