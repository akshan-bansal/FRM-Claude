"""Async client for the WorldMonitor OSINT/news MCP endpoint.

WorldMonitor exposes an MCP server (Streamable-HTTP transport) with ~69 OSINT tools. This is the
thin I/O boundary: it does the ``initialize`` handshake, calls tools, and unwraps the JSON-RPC /
SSE envelope into plain dicts. **It only reads live snapshots** — none of the tools we use here
carry point-in-time history, so downstream code treats WorldMonitor as a live risk overlay, never a
backtestable signal (see :mod:`trading_live_claude.intel.overlay`).

Everything a tool returns is *data*: numeric fields, counts and category labels are extracted here
into a normalized :class:`~trading_live_claude.intel.overlay.IntelSnapshot`. Free-text that rides in
on a headline is never interpreted as an instruction.

The Pro API key is sent as the ``X-WorldMonitor-Key`` header; without it only ``get_sources`` works.
The key comes from settings (``worldmonitor_api_key``) or is passed explicitly. Field names in
:meth:`snapshot` are best-effort against the live payloads and are the one place to tune if a tool's
shape differs — the scoring in :mod:`overlay` is a pure function of the normalized snapshot.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

import httpx
import structlog

from trading_live_claude.config.settings import Settings
from trading_live_claude.intel.events import INTEL_DOMAINS, event_intensity
from trading_live_claude.intel.overlay import IntelSnapshot

log = structlog.get_logger(__name__)

DEFAULT_URL = "https://api.worldmonitor.app/mcp"

# Live market proxies the overlay reads. These Yahoo-style tickers are what get_market_data expects;
# adjust here if the live tool wants different symbols.
MARKET_PROBES = {"equity_vol": "^VIX", "dxy": "DX-Y.NYB", "crypto": "BTC-USD"}


class WorldMonitorError(RuntimeError):
    """A WorldMonitor tool call failed or returned an error envelope."""


class WorldMonitorClient:
    """Minimal MCP-over-HTTP client for WorldMonitor. Use as an async context manager."""

    def __init__(self, api_key: str | None = None, *, url: str = DEFAULT_URL,
                 timeout: float = 40.0) -> None:
        self._key = api_key if api_key is not None else Settings().worldmonitor_api_key
        self._url = url
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None

    @property
    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self._key:
            h["X-WorldMonitor-Key"] = self._key
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    async def __aenter__(self) -> WorldMonitorClient:
        self._client = httpx.AsyncClient(timeout=self._timeout)
        await self._initialize()
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _decode(resp: httpx.Response) -> dict[str, Any]:
        """Unwrap a JSON-RPC response delivered either as JSON or as a text/event-stream body."""
        if "text/event-stream" in resp.headers.get("content-type", ""):
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload:
                        obj: dict[str, Any] = json.loads(payload)
                        if "result" in obj or "error" in obj:
                            return obj
            raise WorldMonitorError("no JSON-RPC data frame in SSE response")
        parsed: dict[str, Any] = resp.json()
        return parsed

    async def _rpc(self, method: str, params: dict[str, Any] | None, rid: int | None) -> dict[str, Any]:
        assert self._client is not None, "use WorldMonitorClient as an async context manager"
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if rid is not None:
            body["id"] = rid
        if params is not None:
            body["params"] = params
        resp = await self._client.post(self._url, headers=self._headers, json=body)
        resp.raise_for_status()
        if rid is None:  # notification — no response body expected
            return {}
        return self._decode(resp)

    async def _initialize(self) -> None:
        assert self._client is not None
        resp = await self._client.post(self._url, headers=self._headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "frm-intel", "version": "0.1"}},
        })
        resp.raise_for_status()
        self._session_id = resp.headers.get("mcp-session-id")
        self._decode(resp)
        await self._rpc("notifications/initialized", {}, None)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call an MCP tool and return its parsed JSON content (or raise WorldMonitorError)."""
        out = await self._rpc("tools/call", {"name": name, "arguments": arguments or {}}, 3)
        if "error" in out:
            raise WorldMonitorError(f"{name}: {out['error']}")
        blocks = out.get("result", {}).get("content", [])
        for b in blocks:
            if b.get("type") == "text":
                try:
                    return json.loads(b["text"])
                except json.JSONDecodeError:
                    return b["text"]
        return out.get("result")

    # ---- normalized snapshot -------------------------------------------------
    async def snapshot(self, *, countries: tuple[str, ...] = ()) -> IntelSnapshot:
        """Fetch the live intelligence bundle and normalize it into an IntelSnapshot.

        Any single tool that errors is tolerated: its features fall back to neutral and ``degraded``
        is set, so the overlay applies a conservative cap rather than trusting a partial read.
        """
        degraded = False

        async def _try(name: str, args: dict[str, Any] | None = None) -> Any:
            nonlocal degraded
            try:
                return await self.call_tool(name, args)
            except (WorldMonitorError, httpx.HTTPError) as e:
                log.warning("worldmonitor.tool_failed", tool=name, error=str(e))
                degraded = True
                return None

        ages: dict[str, float] = {}

        def _age_of(payload: Any, key: str) -> None:
            """Record how old a tool's CACHED payload is; the vendor stamps every one with cached_at."""
            if not isinstance(payload, dict):
                return
            stamp = payload.get("cached_at")
            if not stamp:
                return
            try:
                when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            except ValueError:
                return
            ages[key] = max(0.0, (datetime.now(UTC) - when).total_seconds() / 3600.0)

        news = await _try("get_news_intelligence", {"limit": 40})
        conflict = await _try("get_conflict_events", {"summary": True})
        # natural_disasters overflows the 128KB budget; project to counts with jmespath.
        disasters = await _try("get_natural_disasters",
                               {"jmespath": "data.{earthquakes: length(earthquakes), "
                                            "fires: length(fires), events: length(events)}"})
        energy = await _try("get_energy_intelligence", {"summary": True})
        market = await _try("get_market_data", {"symbols": list(MARKET_PROBES.values())})

        # Event-flow intelligence from the intel archive. Short retention (~weeks), so it is used
        # only as a recent-vs-baseline acceleration tilt — never as a validated signal.
        accel: dict[str, float] = {}
        for dom in INTEL_DOMAINS:
            payload = await _try("get_intel_timeline", {"domain": dom})
            recs = (_unwrap(payload) or {}).get("records", []) if payload else []
            if recs:
                accel[dom] = round(event_intensity(recs, domain=dom).acceleration, 4)

        for payload, key in ((news, "news"), (conflict, "conflict"), (disasters, "disasters"),
                             (energy, "energy"), (market, "market")):
            _age_of(payload, key)
        # the intel archive is queried live per domain, so event features are as fresh as this call
        ages.setdefault("events", 0.0)

        snap = _build_snapshot(news, conflict, disasters, energy, market, countries, degraded)
        snap = replace(snap, event_acceleration=accel, source_age_hours=ages)
        # Per-event decomposition into the graph journal — separate call site from
        # append_snapshot so a failure never breaks the fetch, and the raw records are dropped
        # after this rather than smuggled into IntelSnapshot (which is a frozen public API).
        try:
            _write_event_edges(snap, news, conflict)
        except Exception:
            log.warning("worldmonitor.event_edges_failed")
        return snap


def _write_event_edges(snap: IntelSnapshot, news: Any, conflict: Any) -> None:
    """Persist per-event edges from the raw vendor payloads into ``state/intel_graph.jsonl``.

    Records the vendor's cross-source signals (news) and strategic-risk sample (conflict) — the
    two payload branches that carry actual record shapes with source attribution. Disasters comes
    back as counts only after the jmespath projection, so it has nothing per-event to decompose.
    """
    from trading_live_claude.intel.graph import (
        append_edges,
        event_records_to_edges,
    )
    ts = snap.as_of.isoformat() if snap.as_of else ""
    poll_id = ts
    edges = []

    if isinstance(news, dict):
        nd = news.get("data") if isinstance(news.get("data"), dict) else news
        if isinstance(nd, dict):
            signals = (nd.get("cross-source-signals", {}) or {}).get("signals", []) or []
            if isinstance(signals, list):
                edges.extend(event_records_to_edges(
                    signals, domain="news_signal", poll_id=poll_id, as_of=ts))
            advisories = (nd.get("advisories-bootstrap", {}) or {}).get("advisories", []) or []
            if isinstance(advisories, list):
                edges.extend(event_records_to_edges(
                    advisories, domain="advisory", poll_id=poll_id, as_of=ts))

    if isinstance(conflict, dict):
        cd = conflict.get("data") if isinstance(conflict.get("data"), dict) else conflict
        if isinstance(cd, dict):
            sample = (cd.get("scores", {}) or {}).get("strategicRisks", {}).get("sample", []) or []
            if isinstance(sample, list):
                edges.extend(event_records_to_edges(
                    sample, domain="conflict", poll_id=poll_id, as_of=ts))

    if edges:
        append_edges(edges)


def _num(obj: Any, *keys: str, default: float = 0.0) -> float:
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, (int, float)):
                return float(v)
    return default


def _unwrap(obj: Any) -> dict[str, Any]:
    """WorldMonitor tools wrap their payload in ``{cached_at, stale, data}`` — return the ``data``."""
    if isinstance(obj, dict):
        d = obj.get("data")
        return d if isinstance(d, dict) else obj
    return {}


def _build_snapshot(news: Any, conflict: Any, disasters: Any, energy: Any, market: Any,
                    countries: tuple[str, ...], degraded: bool) -> IntelSnapshot:
    """Extract scoring features from the real WorldMonitor payloads.

    The overlay is driven by WorldMonitor's *calibrated indices* — the geopolitical strategic-risk
    score (0-100) and the market fear/greed composite (0-100) — plus VIX and the count of
    high-severity cross-source signals. Raw catalogue counts (dozens of always-active UCDP conflicts,
    the full quake feed) are deliberately NOT used as drivers: they are baseline noise that would peg
    the gates. Field names are defensive so a shape drift degrades gracefully rather than crashing.
    """
    nd = _unwrap(news)
    insights = nd.get("insights", {}) if isinstance(nd.get("insights"), dict) else {}
    stories = insights.get("topStories", []) or []
    max_importance = 0.0
    for s in stories:
        if isinstance(s, dict):
            imp = _num(s, "upstreamImportanceScore", "importanceScore", "importance")
            max_importance = max(max_importance, imp / 100.0 if imp > 1.5 else imp)

    signals = (nd.get("cross-source-signals", {}) or {}).get("signals", []) or []
    high_alerts = critical = 0
    for sig in signals:
        if isinstance(sig, dict):
            sev = _num(sig, "severityScore")
            high_alerts += sev >= 7.0
            critical += sev >= 8.0

    country_alerts: dict[str, int] = {}
    wanted = {c.upper() for c in countries}
    for a in (nd.get("advisories-bootstrap", {}) or {}).get("advisories", []) or []:
        if isinstance(a, dict):
            cc = str(a.get("country") or a.get("sourceCountry") or "").upper()
            if cc and (not wanted or cc in wanted):
                country_alerts[cc] = country_alerts.get(cc, 0) + 1

    cd = _unwrap(conflict)
    strat = (cd.get("scores", {}) or {}).get("strategicRisks", {}) or {}
    strat_sample = strat.get("sample") or []
    strat_score = 0.0
    for r in strat_sample:
        if isinstance(r, dict) and str(r.get("region", "")).lower() == "global":
            strat_score = _num(r, "score")
    if not strat_score and strat_sample and isinstance(strat_sample[0], dict):
        strat_score = _num(strat_sample[0], "score")

    ed = _unwrap(energy)
    shortages = int(_num((ed.get("fuel-shortages", {}) or {}).get("shortages", {}), "count"))
    energy_stress = min(0.4, shortages / 100.0)  # conservative proxy from a curated shortage list

    md = _unwrap(market)
    fear_greed: float | None = None
    fg = _num((md.get("fear-greed", {}) or {}).get("composite", {}), "score", default=-1.0)
    if fg >= 0:
        fear_greed = fg
    mkt: dict[str, float] = {}
    for bucket in ("commodities-bootstrap", "stocks-bootstrap", "crypto"):
        for q in (md.get(bucket, {}) or {}).get("quotes", []) or []:
            if isinstance(q, dict) and str(q.get("symbol")) == "^VIX":
                mkt["equity_vol"] = _num(q, "price", "last", "value")

    return IntelSnapshot(
        global_alert_count=high_alerts,
        global_max_importance=max_importance,
        country_alert_counts=country_alerts,
        conflict_events_active=critical,          # calibrated escalations, not the raw UCDP count
        energy_stress=energy_stress,
        strategic_risk=strat_score,
        fear_greed=fear_greed,
        market=mkt,
        degraded=degraded,
    )
