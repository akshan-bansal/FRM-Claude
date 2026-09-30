"""One port, three brokers: a shared SQLite store, attach-mode wiring, and the per-book feed.

Before 2026-09-29 each paper loop booted its own shim and all three defaulted to port 8787, so
running QT + Kraken with ``--require-card`` collided; and ``scripts/approval_shim.py`` built an
in-memory store, so a standalone shim could not see the prompts the loops wrote to
``state/approval.db``.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from trading_live_claude.execution.approval_asgi import BookRef, create_app
from trading_live_claude.execution.approval_sqlite import SqliteApprovalStore, SqliteCardRegistry


def _store(db: Path) -> tuple[SqliteApprovalStore, SqliteCardRegistry]:
    registry = SqliteCardRegistry(db)
    return SqliteApprovalStore(registry, db), registry


def test_asset_class_defaults_per_venue_and_can_be_overridden() -> None:
    assert BookRef("questrade", "s1").resolved_asset_class == "equity"
    assert BookRef("kraken", "s2").resolved_asset_class == "crypto"
    assert BookRef("ib", "s3").resolved_asset_class == "multi"        # IB carries both; no guess
    assert BookRef("ib", "s3", asset_class="derivatives").resolved_asset_class == "derivatives"
    assert BookRef("mystery", "s4").resolved_asset_class == "unknown"


def test_books_route_is_denormalized_by_venue_and_never_summed(tmp_path: Path) -> None:
    """Panel #1's feed: one row per book, each with its own venue, class and currency."""
    store, registry = _store(tmp_path / "approval.db")
    books = [BookRef("questrade", "qt-session", "CAD", "equity"),
             BookRef("kraken", "kr-session", "USD", "crypto"),
             BookRef("ib", "ib-session", "USD", "derivatives")]
    app = create_app(store, registry, auth_token=None, state_dir=tmp_path, books=books)
    body = TestClient(app).get("/v1/books").json()
    assert [b["venue"] for b in body["books"]] == ["questrade", "kraken", "ib"]
    assert [b["asset_class"] for b in body["books"]] == ["equity", "crypto", "derivatives"]
    assert [b["currency"] for b in body["books"]] == ["CAD", "USD", "USD"]
    # No aggregate: mixing a CAD equity book with a USD crypto book would be a fiction, and the
    # response says so rather than leaving the reader to assume.
    assert "aggregate" not in body
    assert "no cross-book aggregate" in " ".join(body["notes"])


def test_a_book_with_no_journal_rows_is_unread_not_zero(tmp_path: Path) -> None:
    store, registry = _store(tmp_path / "approval.db")
    app = create_app(store, registry, auth_token=None, state_dir=tmp_path,
                     books=[BookRef("kraken", "never-traded", "USD")])
    from trading_live_claude.audit.meters import CASH, NAV

    row = TestClient(app).get("/v1/books").json()["books"][0]
    readings = row["meters"]["readings"]
    # Approval-database meters still read (they span every session); the BOOK's numbers do not
    # appear at all rather than appearing as zeros.
    assert NAV not in readings and CASH not in readings
    assert any("unread, not zero" in n for n in row["meters"]["notes"])


def test_books_route_without_context_explains_itself(tmp_path: Path) -> None:
    store, registry = _store(tmp_path / "approval.db")
    app = create_app(store, registry, auth_token=None)          # no state_dir, no books
    body = TestClient(app).get("/v1/books").json()
    assert body["books"] == []
    assert "cannot say which books" in " ".join(body["notes"])


def test_a_single_session_id_still_yields_one_book(tmp_path: Path) -> None:
    """Back-compat: a shim started the old way (one session) keeps working."""
    store, registry = _store(tmp_path / "approval.db")
    app = create_app(store, registry, auth_token=None, state_dir=tmp_path,
                     session_id="legacy-session", account_currency="CAD")
    books = TestClient(app).get("/v1/books").json()["books"]
    assert len(books) == 1
    assert books[0]["session_id"] == "legacy-session" and books[0]["currency"] == "CAD"
    assert books[0]["venue"] == "unknown"        # the old call never said which venue


def test_attach_mode_binds_no_port_and_still_gates(tmp_path: Path) -> None:
    """``--card-attach`` wraps the router without starting a server."""
    from trading_live_claude.execution.approval import wire_card_approval
    from trading_live_claude.execution.journal import OrderJournal
    from trading_live_claude.execution.router import Router
    from trading_live_claude.risk import KillSwitch
    from trading_live_claude.risk.heat import PortfolioHeat

    class _B:
        venue = "kraken"
        name = "fake"

        def positions(self, account_number: str) -> list[object]:
            return []

        def place_order(self, order: object) -> object:
            return order

        def cancel_order(self, *a: object, **k: object) -> None:
            pass

    inner = Router(mode="paper", broker=_B(), journal=OrderJournal(tmp_path),  # type: ignore[arg-type]
                   kill_switch=KillSwitch(tmp_path), heat=PortfolioHeat(cap_pct=0.05))
    wiring = wire_card_approval(inner, db_path=tmp_path / "approval.db", start_shim=False,
                               state_dir=tmp_path, session_id="attached")
    assert wiring.shim_thread is None                 # nothing was bound
    assert wiring.router is not inner                 # but the card gate is in the path
    assert wiring.store is not None


def test_two_loops_sharing_one_db_see_each_others_prompts(tmp_path: Path) -> None:
    """The point of the shared store: one shim answers for prompts written by another process."""
    db = tmp_path / "approval.db"
    writer_store, writer_registry = _store(db)          # stands in for the Kraken loop
    reader_store, reader_registry = _store(db)          # stands in for the standalone shim
    app = create_app(reader_store, reader_registry, auth_token=None, state_dir=tmp_path,
                     books=[BookRef("kraken", "kr", "USD")])
    before = TestClient(app).get("/v1/intents/pending").json()["prompts"]
    assert before == []                                 # a fresh shared store, no specimen queue
    assert writer_store is not reader_store             # distinct handles, one file
    assert db.exists()
