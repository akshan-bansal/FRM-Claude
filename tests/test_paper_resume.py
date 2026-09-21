"""Tests for PaperBroker.resume — rebuilding a session's book from its journals (2026-09-21)."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_paper_broker_journal import _order, _StaticFeed
from trading_live_claude.brokers.models import OrderAction
from trading_live_claude.brokers.paper import PaperBroker


def _trade(tmp_path: Path, feed: _StaticFeed) -> PaperBroker:
    """A session that buys, adds, partly sells, closes one name and leaves another open."""
    pb = PaperBroker(feed=feed, starting_equity=50_000.0, journal_dir=tmp_path)
    pb.place_order(_order("AAA", OrderAction.BUY, 100))
    feed._prices["AAA"] = 105.0
    pb.place_order(_order("AAA", OrderAction.BUY, 50))          # add at a new price -> avg entry moves
    pb.place_order(_order("BBB", OrderAction.BUY, 20))
    feed._prices["BBB"] = 210.0
    pb.place_order(_order("BBB", OrderAction.SELL, 20))         # closed: realized P&L
    pb.place_order(_order("AAA", OrderAction.SELL, 30))         # partial trim of the open name
    return pb


def test_resume_rebuilds_the_same_book(tmp_path: Path) -> None:
    feed = _StaticFeed({"AAA": 100.0, "BBB": 200.0})
    orig = _trade(tmp_path, feed)
    new = PaperBroker(feed=feed, starting_equity=50_000.0, journal_dir=tmp_path,
                      session_id=orig.session_id)
    summary = new.resume()
    assert summary["fills_replayed"] == 5
    assert new._cash == pytest.approx(orig._cash)
    assert new._realized_pnl == pytest.approx(orig._realized_pnl)
    assert {s: p.openQuantity for s, p in new._positions.items()} == {"AAA": 120}
    assert new._positions["AAA"].averageEntryPrice == pytest.approx(orig._positions["AAA"].averageEntryPrice)
    assert new._peak_equity == pytest.approx(orig._peak_equity)


def test_resumed_session_continues_order_ids_and_journals_under_the_same_id(tmp_path: Path) -> None:
    feed = _StaticFeed({"AAA": 100.0, "BBB": 200.0})
    orig = _trade(tmp_path, feed)
    new = PaperBroker(feed=feed, starting_equity=50_000.0, journal_dir=tmp_path,
                      session_id=orig.session_id)
    new.resume()
    placed = new.place_order(_order("AAA", OrderAction.SELL, 120))    # close the carried position
    assert placed.id == 6                                               # continues after 5
    assert new._positions == {}
    # and a second resume replays all six fills to a flat book
    again = PaperBroker(feed=feed, starting_equity=50_000.0, journal_dir=tmp_path,
                        session_id=orig.session_id)
    assert again.resume()["positions"] == {}


def test_wrong_starting_equity_is_refused_not_guessed(tmp_path: Path) -> None:
    feed = _StaticFeed({"AAA": 100.0, "BBB": 200.0})
    orig = _trade(tmp_path, feed)
    new = PaperBroker(feed=feed, starting_equity=100_000.0, journal_dir=tmp_path,
                      session_id=orig.session_id)
    with pytest.raises(PaperBroker.RehydrationMismatch, match="starting equity"):
        new.resume()


def test_other_venue_and_unknown_session_are_refused(tmp_path: Path) -> None:
    feed = _StaticFeed({"AAA": 100.0, "BBB": 200.0})
    orig = _trade(tmp_path, feed)
    wrong = PaperBroker(feed=feed, starting_equity=50_000.0, journal_dir=tmp_path,
                        session_id=orig.session_id, venue="kraken")
    with pytest.raises(PaperBroker.RehydrationMismatch, match="traded on"):
        wrong.resume()
    ghost = PaperBroker(feed=feed, starting_equity=50_000.0, journal_dir=tmp_path,
                        session_id="0" * 32)
    with pytest.raises(PaperBroker.RehydrationMismatch, match="no journal rows"):
        ghost.resume()


def test_order_ids_are_per_instance_not_shared(tmp_path: Path) -> None:
    """Audit gap (2026-09-18): the counter was a class variable shared by every PaperBroker."""
    feed = _StaticFeed({"AAA": 100.0})
    a = PaperBroker(feed=feed, starting_equity=10_000.0, journal_dir=tmp_path / "a")
    b = PaperBroker(feed=feed, starting_equity=10_000.0, journal_dir=tmp_path / "b")
    assert a.place_order(_order("AAA", OrderAction.BUY, 1)).id == 1
    assert b.place_order(_order("AAA", OrderAction.BUY, 1)).id == 1
    assert a.place_order(_order("AAA", OrderAction.BUY, 1)).id == 2
