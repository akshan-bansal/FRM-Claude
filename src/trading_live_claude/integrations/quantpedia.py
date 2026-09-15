"""Quantpedia API client (https://quantpedia.com/api/v1/docs) — read-only research data.

Endpoints, as documented: ``GET /api/v1/strategy`` (id, name, path), ``/strategy/{id}`` (metadata:
paperMetrics, calculatedMetrics, instruments, marketFactors, rebalancingPeriod, sourcePaper, …),
``/strategy/{id}/performance`` (date-indexed series), ``/strategy/{id}/source-code`` (backtest files)
and ``/paper/{id}`` (PDF). HTTP Basic auth with username + API key (Quantpedia Pro). Quantpedia's
curves are research evidence, not signals: anything traded must still clear the walk-forward.
"""
from __future__ import annotations

from typing import Any

import httpx
import pandas as pd

QUANTPEDIA_BASE = "https://quantpedia.com/api/v1"


class QuantpediaError(RuntimeError):
    """A Quantpedia API call failed (auth, access rights, missing strategy, transport)."""


_STATUS = {401: "invalid credentials", 403: "insufficient access rights (Quantpedia Pro API access?)",
           404: "not found or no access"}


class QuantpediaClient:
    def __init__(self, username: str, api_key: str, *, base_url: str = QUANTPEDIA_BASE,
                 client: httpx.Client | None = None, timeout: float = 30.0) -> None:
        if not username or not api_key:
            raise QuantpediaError("Set QUANTPEDIA_USERNAME and QUANTPEDIA_API_KEY in .env.")
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._auth = httpx.BasicAuth(username, api_key)

    def _get(self, path: str, **params: str) -> httpx.Response:
        try:
            r = self._client.get(f"{self.base_url}{path}", params=params or None, auth=self._auth)
        except httpx.HTTPError as e:
            raise QuantpediaError(f"GET {path}: {e}") from e
        if r.status_code != 200:
            raise QuantpediaError(f"GET {path}: HTTP {r.status_code} {_STATUS.get(r.status_code, '')}".strip())
        return r

    def list_strategies(self) -> list[dict[str, Any]]:
        data = self._get("/strategy").json()
        return list(data) if isinstance(data, list) else []

    def strategy(self, strategy_id: str) -> dict[str, Any]:
        data = self._get(f"/strategy/{strategy_id}").json()
        return dict(data) if isinstance(data, dict) else {}

    def performance(self, strategy_id: str) -> pd.Series:
        """The strategy's performance curve as a date-indexed float Series."""
        rows = self._get(f"/strategy/{strategy_id}/performance", orient="records").json()
        if not rows:
            return pd.Series(dtype=float, name=strategy_id)
        frame = pd.DataFrame(rows)
        return pd.Series(frame["performance"].astype(float).to_numpy(),
                         index=pd.to_datetime(frame["date"]), name=strategy_id).sort_index()

    def source_code(self, strategy_id: str) -> list[dict[str, Any]]:
        data = self._get(f"/strategy/{strategy_id}/source-code").json()
        return list(data) if isinstance(data, list) else []

    def paper_pdf(self, file_id: str) -> bytes:
        return self._get(f"/paper/{file_id}").content

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> QuantpediaClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def matches(detail: dict[str, Any], terms: list[str]) -> bool:
    """True when every term appears in the strategy's name, description, keywords, instruments or factors."""
    haystack = " ".join([
        str(detail.get("name", "")), str(detail.get("description", "")),
        *map(str, detail.get("keywords") or []), *map(str, detail.get("instruments") or []),
        *map(str, detail.get("marketFactors") or []),
    ]).lower()
    return all(t.lower() in haystack for t in terms)
