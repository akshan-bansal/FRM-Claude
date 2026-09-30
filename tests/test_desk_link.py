"""The desk panel's live link: the shim serves the page, and the page reads the running session.

What these cover, in the order the wiring runs:

1. ``GET /desk`` is the unlock shell — public, carries no journal data and never the token.
2. ``GET /v1/desk/page`` is the built page, behind the same auth as every other ``/v1`` route.
3. The legacy-path middleware does not rewrite ``/desk`` into ``/v1/desk``.
4. A passbook row carries ``issued_at``, so card response time is measurable rather than asserted.

The fourth is a regression guard with history: ``get_avg_ttl_response`` returned the literal 4.2
in every case until 2026-09-28 — the loop meant to compute it was a ``pass`` — and the desk panel
displayed that constant as "median card response time".
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from trading_live_claude.brokers.models import OrderAction
from trading_live_claude.execution.approval import (
    CardRegistry,
    InMemoryApprovalStore,
    PassbookEntry,
)
from trading_live_claude.execution.approval_asgi import create_app
from trading_live_claude.execution.approval_metrics import ApprovalMetrics
from trading_live_claude.execution.router import OrderIntent

PAGE = "<title>QuantPort.io</title><script>const DATA = {};</script>"


def intent(symbol: str = "PAXG/USD") -> OrderIntent:
    return OrderIntent(symbol=symbol, action=OrderAction.BUY, shares=1.5, entry=100.0,
                       stop=95.0, target=120.0, strategy="ts_momentum", risk_dollars=7.5,
                       account_number="PAPER-001")


@pytest.fixture
def built_page(tmp_path: Path) -> Path:
    page = tmp_path / "desk.html"
    page.write_text(PAGE, encoding="utf-8")
    return page


def client(page: Path | None, token: str | None = None) -> TestClient:
    registry = CardRegistry()
    store = InMemoryApprovalStore(registry)
    return TestClient(create_app(store, registry, desk_page=page, auth_token=token))


# --------------------------------------------------------------------------- #
# the shell                                                                    #
# --------------------------------------------------------------------------- #

def test_shell_is_public_and_leaks_nothing(built_page: Path) -> None:
    """The shell is reachable without a token and must not hand one out.

    It exists so the token can be supplied by the viewer instead of travelling in a URL, which
    means it has to be readable by an unauthenticated browser — so it must carry no journal data
    and no secret of its own.
    """
    body = client(built_page, token="s3cr3t").get("/desk").text
    assert "QUANTPORT" in body
    assert "s3cr3t" not in body
    assert "DATA" not in body          # no baked page content in the public shell
    assert '"auth_required": true' in body or '"auth_required":true' in body


def test_shell_script_is_scoped(built_page: Path) -> None:
    """The shell hands the document to the page with document.write, which keeps this Window.

    Anything the shell declares at top level would still be declared when the page's own script
    runs, and two ``const $`` in one scope is a SyntaxError that kills the whole panel. It bit us
    once; the shell body stays inside an IIFE.
    """
    body = client(built_page).get("/desk").text
    assert "<script>(function(){" in body
    assert "})();</script>" in body


def test_shell_is_not_rewritten_by_the_legacy_middleware(built_page: Path) -> None:
    """``/desk`` is a bootstrap surface, not a legacy unversioned call."""
    r = client(built_page).get("/desk")
    assert r.status_code == 200
    assert "QuantPort.io" in r.text


# --------------------------------------------------------------------------- #
# the page                                                                     #
# --------------------------------------------------------------------------- #

def test_page_is_served_to_an_authorised_reader(built_page: Path) -> None:
    r = client(built_page, token="tok").get("/v1/desk/page", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200
    assert r.text == PAGE
    assert r.headers["cache-control"] == "no-store"


def test_page_needs_the_token_when_the_shim_has_one(built_page: Path) -> None:
    """The page is journal-derived, so reaching the port must not be enough to read it."""
    assert client(built_page, token="tok").get("/v1/desk/page").status_code == 401


def test_page_is_open_when_the_shim_is_open(built_page: Path) -> None:
    assert client(built_page).get("/v1/desk/page").status_code == 200


def test_missing_page_says_how_to_build_it(tmp_path: Path) -> None:
    r = client(tmp_path / "never-built.html").get("/v1/desk/page")
    assert r.status_code == 404
    assert "build_desk_dash" in r.json()["build"]


# --------------------------------------------------------------------------- #
# what the page reads while a session runs                                     #
# --------------------------------------------------------------------------- #

def test_pending_prompts_are_visible_to_the_panel(built_page: Path) -> None:
    registry = CardRegistry()
    store = InMemoryApprovalStore(registry)
    app = create_app(store, registry, desk_page=built_page)
    prompt = store.publish(intent(), broker="paper", mode="paper", ttl_seconds=90.0)
    body = TestClient(app).get("/v1/intents/pending").json()
    rows = body["prompts"] if "prompts" in body else body["items"]
    assert [r["intent_id"] for r in rows] == [prompt.intent_id]
    # the fingerprint the panel prints is the one the store served, not one recomputed beside it
    assert rows[0]["fingerprint"] == prompt.fingerprint


def test_response_time_is_measured_not_asserted() -> None:
    """A median over real intervals — and never the old 4.2 constant."""
    registry = CardRegistry()
    store = InMemoryApprovalStore(registry)
    now = datetime.now(UTC)
    store._passbook.extend([                                    # fixture-style seed
        PassbookEntry(intent_id="a", resolved_at=now, verdict="ACCEPT", broker="paper",
                      symbol="PAXG/USD", action="Buy", shares=1.0, notional_usd=10.0,
                      strategy="s", thesis="", intel_ref="", card_id="c1",
                      issued_at=now - timedelta(seconds=2)),
        PassbookEntry(intent_id="b", resolved_at=now, verdict="DECLINE", broker="paper",
                      symbol="PAXG/USD", action="Buy", shares=1.0, notional_usd=10.0,
                      strategy="s", thesis="", intel_ref="", card_id="c1",
                      issued_at=now - timedelta(seconds=6)),
        # EXPIRED carries the sweep's timestamp, not a decision: it must not enter the median
        PassbookEntry(intent_id="c", resolved_at=now, verdict="EXPIRED", broker="paper",
                      symbol="PAXG/USD", action="Buy", shares=1.0, notional_usd=10.0,
                      strategy="s", thesis="", intel_ref="", card_id=None,
                      issued_at=now - timedelta(seconds=900)),
    ])
    metrics = ApprovalMetrics(store, journal=None, router=None)
    assert metrics.get_avg_ttl_response() == 4.0       # median of 2s and 6s
    assert metrics.get_avg_ttl_response() != 4.2       # the constant this replaced


def test_response_time_is_null_when_nothing_was_decided() -> None:
    """The median of no observations is unknown. It is not zero, and it is not 4.2."""
    registry = CardRegistry()
    store = InMemoryApprovalStore(registry)
    metrics = ApprovalMetrics(store, journal=None, router=None)
    assert metrics.get_avg_ttl_response() is None


def test_passbook_carries_the_issue_time_end_to_end() -> None:
    """A real signed ACCEPT keeps the timestamp the latency is measured from.

    End to end through the store the card actually talks to: publish, sign the canonical bytes,
    respond, read the passbook. Without ``issued_at`` on that row the response time can only be
    guessed, which is how the constant got there in the first place.
    """
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    registry = CardRegistry()
    registry.register("c1", pem)
    store = InMemoryApprovalStore(registry)
    prompt = store.publish(intent(), broker="paper", mode="paper", ttl_seconds=90.0)
    assert store.respond(prompt.intent_id, decision="ACCEPT", card_id="c1",
                         signature=key.sign(prompt.canonical.encode())) is True

    entry = store.passbook(limit=5)[0]
    assert entry.intent_id == prompt.intent_id
    assert entry.issued_at == prompt.issued_at
    assert entry.resolved_at >= prompt.issued_at

    metrics = ApprovalMetrics(store, journal=None, router=None)
    took = metrics.get_avg_ttl_response()
    assert took is not None
    assert 0.0 <= took < 5.0          # measured from these two stamps, not a constant
