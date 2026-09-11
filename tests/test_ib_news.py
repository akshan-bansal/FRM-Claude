"""Tests for IBBroker's news surface + its round-trip through intel.graph.

Covers what can be exercised without a running TWS / IB Gateway or ib_insync installed:

* the module-level record projection (``_news_tick_to_record``),
* buffer semantics (subscribe/drain/dedup) via a fake ib_insync module,
* the end-to-end fixture → ``event_records_to_edges`` → ``append_edges`` round-trip
  landing the expected predicates in a temp graph journal file.

Live-connection paths and paid market-data calls are deliberately NOT tested here — a
running TWS with news subscriptions is an integration-test dependency, not a CI one.
"""
from __future__ import annotations

import json
import sys
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_live_claude.brokers.ib import IBBroker, _news_tick_to_record
from trading_live_claude.intel.graph import (
    append_edges,
    edges_where,
    event_records_to_edges,
    load_edges,
)

# ---- _news_tick_to_record ---------------------------------------------------


def test_record_shape_matches_event_records_contract() -> None:
    """The dict must carry every field event_records_to_edges' helpers probe for."""
    rec = _news_tick_to_record(
        provider_code="BRFG", article_id="b$abc123", headline="AAPL beats estimates",
        time_stamp=1710000000, extra_data="body:...", symbol="AAPL",
    )
    assert rec["id"] == "BRFG:b$abc123"
    assert rec["title"] == "AAPL beats estimates"
    assert rec["headline"] == "AAPL beats estimates"
    assert rec["sources"] == ["BRFG"]
    # ingestedAt must be ISO-8601 with a tz so _event_id's fallback hash is stable
    parsed = datetime.fromisoformat(rec["ingestedAt"])
    assert parsed.tzinfo is not None
    assert rec["meta"]["symbol"] == "AAPL"
    assert rec["meta"]["extraData"] == "body:..."


def test_record_handles_ms_epoch_and_datetime_timestamps() -> None:
    """timeStamp can be seconds (NewsTick), milliseconds (defensive), or datetime (historical)."""
    secs = _news_tick_to_record(
        provider_code="X", article_id="1", headline="h", time_stamp=1_700_000_000,
        extra_data=None, symbol=None,
    )
    ms = _news_tick_to_record(
        provider_code="X", article_id="1", headline="h", time_stamp=1_700_000_000_000,
        extra_data=None, symbol=None,
    )
    assert secs["ingestedAt"] == ms["ingestedAt"]

    dt = datetime(2026, 1, 15, 12, 30, tzinfo=UTC)
    from_dt = _news_tick_to_record(
        provider_code="X", article_id="1", headline="h", time_stamp=dt,
        extra_data=None, symbol=None,
    )
    assert from_dt["ingestedAt"].startswith("2026-01-15T12:30")


def test_record_ident_blank_when_no_article_id_leaves_fallback_hash_to_graph() -> None:
    """No articleId → id is empty; _event_id falls back to hash(title|ingestedAt)."""
    rec = _news_tick_to_record(
        provider_code="X", article_id="", headline="A headline with no id",
        time_stamp=1710000000, extra_data=None, symbol=None,
    )
    assert rec["id"] == ""
    # event_records_to_edges must still produce edges via the hash fallback
    edges = event_records_to_edges([rec], domain="ib_news",
                                     poll_id="p1", as_of="2026-09-10T00:00:00+00:00")
    assert edges, "hash fallback should have yielded an event id"
    assert any(e.subject[0] == "event" for e in edges)


# ---- subscribe / drain ------------------------------------------------------


_FakeNewsTick = namedtuple(
    "NewsTick", ["timeStamp", "providerCode", "articleId", "headline", "extraData"]
)


class _FakeEvent:
    """Minimal stand-in for ib_insync's Event class — supports += to add handlers."""

    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def emit(self, *args) -> None:
        for h in self.handlers:
            h(*args)


class _FakeIB:
    """Fake ib_insync.IB just for the news surface. reqMktData records calls; the rest
    is a no-op. tickNewsEvent is where the test fires simulated NewsTicks."""

    def __init__(self) -> None:
        self.tickNewsEvent = _FakeEvent()
        self.newsBulletinEvent = _FakeEvent()
        self.reqMktData_calls: list = []
        self.reqNewsBulletins_called_with: bool | None = None
        self._connected = True
        self.tickle_calls = 0
        self._providers: list = []

    def reqMktData(self, contract, ticks, snapshot, regulatorySnapshot):
        self.reqMktData_calls.append((contract, ticks, snapshot, regulatorySnapshot))
        return object()          # ib_insync returns a Ticker; we don't use it

    def reqNewsBulletins(self, all_msgs: bool) -> None:
        self.reqNewsBulletins_called_with = all_msgs

    def reqNewsProviders(self):
        return self._providers

    def managedAccounts(self):
        return ["DU12345"]

    def disconnect(self) -> None:
        pass


def _install_fake_ib(monkeypatch: pytest.MonkeyPatch, fake_ib: _FakeIB) -> None:
    """Wire the IBBroker's lazy import + connect path to return `fake_ib` without a real
    socket. Uses monkeypatching of sys.modules + the internal _require_ib."""
    def _stock(symbol, exchange, currency):
        return SimpleNamespace(symbol=symbol, exchange=exchange, currency=currency)
    fake_module = SimpleNamespace(IB=lambda: fake_ib, Stock=_stock)
    monkeypatch.setitem(sys.modules, "ib_insync", fake_module)


def _make_broker(monkeypatch: pytest.MonkeyPatch) -> tuple[IBBroker, _FakeIB]:
    fake = _FakeIB()
    _install_fake_ib(monkeypatch, fake)
    broker = IBBroker()
    # Bypass the real socket connect — we only need the fake IB in place.
    broker._ib = fake
    broker._connected = True
    return broker, fake


def test_subscribe_news_registers_tick_handler_once(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, fake = _make_broker(monkeypatch)
    broker.subscribe_news(["AAPL", "MSFT"], providers=["BRFG"])
    # tickNewsEvent handler attached exactly once even after a second subscribe call.
    broker.subscribe_news(["SPY"], providers=["BRFG"])
    assert len(fake.tickNewsEvent.handlers) == 1
    # reqMktData was called for each symbol on each subscribe, with the news tick list.
    assert len(fake.reqMktData_calls) == 3
    for _contract, ticks, snap, reg in fake.reqMktData_calls:
        assert ticks == "mdoff,292"
        assert snap is False and reg is False


def test_drain_news_returns_records_and_clears_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, fake = _make_broker(monkeypatch)
    broker.subscribe_news(["AAPL"], providers=["BRFG"])
    fake.tickNewsEvent.emit(_FakeNewsTick(
        timeStamp=1_710_000_000, providerCode="BRFG", articleId="a1",
        headline="Apple beats", extraData="body:...",
    ))
    fake.tickNewsEvent.emit(_FakeNewsTick(
        timeStamp=1_710_000_100, providerCode="BRFG", articleId="a2",
        headline="Msft launches", extraData="",
    ))
    drained = broker.drain_news()
    assert [r["id"] for r in drained] == ["BRFG:a1", "BRFG:a2"]
    assert broker.drain_news() == []       # second drain yields nothing


def test_subscribe_news_filters_out_non_whitelisted_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, fake = _make_broker(monkeypatch)
    broker.subscribe_news(["AAPL"], providers=["BRFG"])
    fake.tickNewsEvent.emit(_FakeNewsTick(1_710_000_000, "BRFG", "a1", "H1", ""))
    fake.tickNewsEvent.emit(_FakeNewsTick(1_710_000_100, "DJ-N", "b2", "H2", ""))
    fake.tickNewsEvent.emit(_FakeNewsTick(1_710_000_200, "REUTERS", "c3", "H3", ""))
    drained = broker.drain_news()
    assert [r["id"] for r in drained] == ["BRFG:a1"]


def test_subscribe_news_dedups_duplicate_provider_article(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, fake = _make_broker(monkeypatch)
    broker.subscribe_news(["AAPL"], providers=None)
    tick = _FakeNewsTick(1_710_000_000, "BRFG", "same-id", "H", "")
    fake.tickNewsEvent.emit(tick)
    fake.tickNewsEvent.emit(tick)             # exact duplicate — one graph edge, not two
    fake.tickNewsEvent.emit(tick)
    drained = broker.drain_news()
    assert len(drained) == 1


def test_subscribe_news_drops_blank_headline_and_blank_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, fake = _make_broker(monkeypatch)
    broker.subscribe_news(["AAPL"], providers=None)
    fake.tickNewsEvent.emit(_FakeNewsTick(1_710_000_000, "BRFG", "a1", "", ""))    # blank headline
    fake.tickNewsEvent.emit(_FakeNewsTick(1_710_000_100, "", "a2", "Hi", ""))       # blank provider
    fake.tickNewsEvent.emit(_FakeNewsTick(1_710_000_200, "BRFG", "a3", "Real", ""))
    drained = broker.drain_news()
    assert [r["id"] for r in drained] == ["BRFG:a3"]


def test_subscribe_bulletins_records_via_bulletin_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, fake = _make_broker(monkeypatch)
    broker.subscribe_news([], providers=None, include_bulletins=True)
    assert fake.reqNewsBulletins_called_with is True
    fake.newsBulletinEvent.emit(SimpleNamespace(
        msgId=42, msgType=1, message="NYSE halt on XYZ", origExchange="NYSE",
    ))
    drained = broker.drain_news()
    assert drained[0]["sources"] == ["IB-BULLETIN"]
    assert drained[0]["id"] == "IB-BULLETIN:42"


def test_list_news_providers_projects_provider_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    broker, fake = _make_broker(monkeypatch)
    fake._providers = [
        SimpleNamespace(code="BRFG", name="Briefing.com General"),
        SimpleNamespace(code="", name="empty code — should be filtered"),
        SimpleNamespace(code="DJ-N", name="Dow Jones News"),
    ]
    out = broker.list_news_providers()
    assert out == [
        {"code": "BRFG", "name": "Briefing.com General"},
        {"code": "DJ-N", "name": "Dow Jones News"},
    ]


# ---- round-trip: fixture records → edges → journal --------------------------


def test_drained_records_round_trip_into_graph_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The final gate: what drain_news emits must produce a well-formed edge journal
    when handed through event_records_to_edges → append_edges."""
    broker, fake = _make_broker(monkeypatch)
    broker.subscribe_news(["AAPL"], providers=None)
    fake.tickNewsEvent.emit(_FakeNewsTick(
        timeStamp=1_710_000_000, providerCode="BRFG", articleId="a1",
        headline="Apple beats estimates on Q4", extraData="",
    ))
    fake.tickNewsEvent.emit(_FakeNewsTick(
        timeStamp=1_710_000_100, providerCode="DJ-N", articleId="b2",
        headline="Fed signals cut", extraData="",
    ))
    recs = broker.drain_news()
    assert len(recs) == 2

    journal = tmp_path / "intel_graph.jsonl"
    edges = event_records_to_edges(recs, domain="ib_news",
                                     poll_id="poll-2026-09-10T00:00:00",
                                     as_of="2026-09-10T00:00:00+00:00")
    append_edges(edges, path=journal)

    loaded = load_edges(journal)
    assert loaded, "journal round-trip should recover edges"
    mentioned = edges_where(loaded, predicate="mentioned_by")
    assert {e.object[1] for e in mentioned} == {"BRFG", "DJ-N"}
    about = edges_where(loaded, predicate="about_domain")
    assert {e.object[1] for e in about} == {"ib_news"}
    observed = edges_where(loaded, predicate="observed",
                            subject=("poll", "poll-2026-09-10T00:00:00"))
    assert len(observed) == 2                                # one poll→event per record
    # affects_region degrades to nothing because IB news carries no country field —
    # this is the documented degradation, not a bug.
    assert edges_where(loaded, predicate="affects_region") == []

    # Journal shape: each line is a JSON object with the Edge.to_row() keys.
    lines = journal.read_text(encoding="utf-8").strip().splitlines()
    assert lines
    sample = json.loads(lines[0])
    for k in ("subject", "predicate", "object", "weight", "as_of", "meta"):
        assert k in sample
