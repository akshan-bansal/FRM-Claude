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


# --- partial-sell realized P&L (fixed 2026-09-24) --------------------------------------------

def test_partial_sell_books_realized_pnl(tmp_path: Path) -> None:
    """Before the fix only full closes realized: trims moved proceeds to cash and realized stayed 0.

    Measured miss on QT session fba831e3 (slot trims, orders 5-8): -$39.92 never booked.
    """
    feed = _StaticFeed({"AAA": 100.0})
    pb = PaperBroker(feed=feed, starting_equity=50_000.0, journal_dir=tmp_path,
                     slippage_bps=0.0, commission_per_trade=0.0)
    pb.place_order(_order("AAA", OrderAction.BUY, 100))          # avg entry 100
    feed._prices["AAA"] = 110.0
    pb.place_order(_order("AAA", OrderAction.SELL, 40))          # trim 40 at 110 -> +400
    assert pb._realized_pnl == pytest.approx(400.0)
    pos = pb._positions["AAA"]
    assert pos.openQuantity == 60
    assert pos.averageEntryPrice == pytest.approx(100.0)         # unchanged by a reduction


def test_partial_then_full_close_sums_to_one_full_close(tmp_path: Path) -> None:
    """Two trims must realize exactly what a single close of the same size would."""
    def book(sells: list[float]) -> float:
        feed = _StaticFeed({"AAA": 100.0})
        pb = PaperBroker(feed=feed, starting_equity=50_000.0, journal_dir=tmp_path,
                         slippage_bps=0.0, commission_per_trade=0.0)
        pb.place_order(_order("AAA", OrderAction.BUY, 100))
        feed._prices["AAA"] = 112.5
        for qty in sells:
            pb.place_order(_order("AAA", OrderAction.SELL, qty))
        return pb._realized_pnl

    assert book([30, 70]) == pytest.approx(book([100]))
    assert book([100]) == pytest.approx(1250.0)                  # (112.5 - 100) * 100


def test_resume_round_trips_a_session_with_partial_sells(tmp_path: Path) -> None:
    """Replay must reproduce the same realized figure, or a restart strands the book."""
    feed = _StaticFeed({"AAA": 100.0, "BBB": 200.0})
    orig = _trade(tmp_path, feed)
    assert orig._realized_pnl != pytest.approx(orig._realized_pnl_full_closes_only)  # trim included
    new = PaperBroker(feed=feed, starting_equity=50_000.0, journal_dir=tmp_path,
                      session_id=orig.session_id)
    new.resume()
    assert new._realized_pnl == pytest.approx(orig._realized_pnl)
    assert new._realized_pnl_full_closes_only == pytest.approx(orig._realized_pnl_full_closes_only)


def test_resume_accepts_a_journal_written_before_the_fix(tmp_path: Path) -> None:
    """A pre-2026-09-24 equity row booked full closes only; refusing it would strand a live book.

    state/ is ground truth, so the row is not rewritten: resume takes the corrected figure and warns.
    """
    feed = _StaticFeed({"AAA": 100.0, "BBB": 200.0})
    orig = _trade(tmp_path, feed)
    legacy = orig._realized_pnl_full_closes_only
    eq = tmp_path / "paper_equity.csv"
    rows = eq.read_text(encoding="utf-8").splitlines()
    head, last = rows[0], rows[-1].split(",")
    last[head.split(",").index("realized_pnl")] = f"{legacy:.4f}"     # emulate the old accounting
    eq.write_text("\n".join([*rows[:-1], ",".join(last)]) + "\n", encoding="utf-8")

    new = PaperBroker(feed=feed, starting_equity=50_000.0, journal_dir=tmp_path,
                      session_id=orig.session_id)
    new.resume()                                                     # must not raise
    assert new._realized_pnl == pytest.approx(orig._realized_pnl)    # corrected, not the legacy value


def test_resume_still_refuses_a_journal_that_matches_neither_accounting(tmp_path: Path) -> None:
    feed = _StaticFeed({"AAA": 100.0, "BBB": 200.0})
    orig = _trade(tmp_path, feed)
    eq = tmp_path / "paper_equity.csv"
    rows = eq.read_text(encoding="utf-8").splitlines()
    head, last = rows[0], rows[-1].split(",")
    last[head.split(",").index("realized_pnl")] = "-9999.00"
    eq.write_text("\n".join([*rows[:-1], ",".join(last)]) + "\n", encoding="utf-8")
    new = PaperBroker(feed=feed, starting_equity=50_000.0, journal_dir=tmp_path,
                      session_id=orig.session_id)
    with pytest.raises(PaperBroker.RehydrationMismatch, match="legacy"):
        new.resume()
