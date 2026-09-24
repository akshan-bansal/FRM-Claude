"""Hash-chained audit ledger (AUDIT_LEDGER_SCOPE.md phase 3).

The chain is only worth having if tampering actually fails verification, so most of this file is
attacks: edit a payload, edit an envelope field, delete a row, reorder two rows, splice one in.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.test_router import _StubBroker, _intent
from trading_live_claude.audit import (
    GENESIS,
    Ledger,
    canonical_json,
    read_stream,
    verify_chain,
)
from trading_live_claude.execution.router import Router


def _ledger(tmp_path: Path, **kw) -> Ledger:
    return Ledger(tmp_path / "ledger", stream=kw.pop("stream", "test"), session_id="s1", **kw)


def _fill(led: Ledger, n: int = 3) -> None:
    for i in range(n):
        led.append("RISK_CHECK", {"i": i, "symbol": "AAPL"}, intent_id=f"i{i}")


# --- envelope --------------------------------------------------------------------------------

def test_envelope_carries_the_scoped_fields(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    row = led.append("INTENT_CREATED", {"symbol": "AAPL", "shares": 10}, intent_id="i1",
                     strategy_id="bollinger")
    assert row is not None
    for field in ("seq", "ts", "event", "mode", "user_id", "device_id", "session_id", "intent_id",
                  "payload", "payload_hash", "prev_hash", "row_hash", "schema"):
        assert field in row, field
    assert row["seq"] == 0 and row["prev_hash"] == GENESIS
    assert row["payload_hash"] == __import__("hashlib").sha256(
        canonical_json(row["payload"])).hexdigest()
    # Phase 4/5 fields exist as nulls now rather than appearing later and changing the shape.
    assert row["strategy_version"] is None and row["risk_check_version"] is None
    assert row["signature"] is None


def test_unknown_event_names_are_refused(tmp_path: Path) -> None:
    """A typo must not silently create a new event type that no report knows to look for."""
    led = _ledger(tmp_path)
    with pytest.raises(ValueError, match="unknown ledger event"):
        led.append("FILLD", {})


def test_canonical_json_is_key_order_independent() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    assert b" " not in canonical_json({"a": [1, 2]})


# --- the chain -------------------------------------------------------------------------------

def test_a_clean_stream_verifies(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    _fill(led, 5)
    assert led.verify() == (True, "ok")
    assert [r["seq"] for r in led.rows()] == [0, 1, 2, 3, 4]


def test_an_edited_payload_is_detected(tmp_path: Path) -> None:
    """The headline property: history cannot be rewritten without it showing."""
    led = _ledger(tmp_path)
    _fill(led, 3)
    path = next((tmp_path / "ledger").glob("*.jsonl"))
    rows = read_stream(path)
    rows[1]["payload"]["symbol"] = "TSLA"                 # someone edits what was traded
    ok, reason = verify_chain(rows)
    assert ok is False and "payload of row 1" in reason


def test_an_edited_envelope_field_is_detected(tmp_path: Path) -> None:
    """Chaining over the whole envelope, not just the payload: a retimed row fails too."""
    led = _ledger(tmp_path)
    _fill(led, 3)
    rows = led.rows()
    rows[1]["ts"] = "2020-01-01T00:00:00+00:00"
    ok, reason = verify_chain(rows)
    assert ok is False and "modified after it was written" in reason


def test_a_deleted_row_is_detected(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    _fill(led, 4)
    rows = led.rows()
    del rows[2]                                           # quietly drop a rejection
    ok, reason = verify_chain(rows)
    assert ok is False and "sequence gap" in reason


def test_reordered_rows_are_detected(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    _fill(led, 4)
    rows = led.rows()
    rows[1], rows[2] = rows[2], rows[1]
    ok, _ = verify_chain(rows)
    assert ok is False


def test_a_spliced_row_is_detected(tmp_path: Path) -> None:
    """Forging a row means forging every row after it — that is the point of the chain."""
    led = _ledger(tmp_path)
    _fill(led, 3)
    rows = led.rows()
    forged = dict(rows[1])
    forged["payload"] = {"i": 99, "symbol": "FAKE"}
    rows.insert(2, forged)
    ok, _ = verify_chain(rows)
    assert ok is False


def test_a_tampered_row_that_recomputes_its_own_hash_still_breaks_the_link(tmp_path: Path) -> None:
    """A careful attacker who fixes one row's own hash still fails: the NEXT row points at the old."""
    from trading_live_claude.audit.ledger import row_hash

    led = _ledger(tmp_path)
    _fill(led, 3)
    rows = led.rows()
    rows[1]["payload"] = {"i": 1, "symbol": "FAKE"}
    rows[1]["payload_hash"] = __import__("hashlib").sha256(
        canonical_json(rows[1]["payload"])).hexdigest()
    rows[1]["row_hash"] = row_hash(rows[1])               # self-consistent now
    ok, reason = verify_chain(rows)
    assert ok is False and "broken link at seq 2" in reason


# --- durability ------------------------------------------------------------------------------

def test_the_chain_continues_across_a_restart(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    _fill(led, 2)
    reopened = _ledger(tmp_path)                          # new process, same stream
    reopened.append("FILLED", {"symbol": "AAPL"}, intent_id="i9")
    assert [r["seq"] for r in reopened.rows()] == [0, 1, 2]
    assert reopened.verify() == (True, "ok")


def test_the_chain_continues_across_a_day_rollover(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    day1 = datetime(2026, 9, 24, 23, 59, tzinfo=UTC)
    led.append("RISK_CHECK", {"i": 0}, at=day1)
    led.append("FILLED", {"i": 1}, at=day1 + timedelta(minutes=2))
    files = sorted(p.name for p in (tmp_path / "ledger").glob("*.jsonl"))
    assert len(files) == 2                                # rolled into a new day file
    assert led.verify() == (True, "ok")                   # but one unbroken chain


def test_streams_are_independent(tmp_path: Path) -> None:
    """Concurrent books must not corrupt each other's chains — that is why chains are per stream."""
    qt = _ledger(tmp_path, stream="qt")
    kraken = _ledger(tmp_path, stream="kraken")
    for i in range(3):
        qt.append("RISK_CHECK", {"i": i})
        kraken.append("RISK_CHECK", {"i": i})
    assert qt.verify() == (True, "ok") and kraken.verify() == (True, "ok")
    assert {r["stream"] for r in qt.rows()} == {"qt"}
    assert len(qt.rows()) == 3 and len(kraken.rows()) == 3


def test_a_torn_final_line_does_not_poison_the_stream(tmp_path: Path) -> None:
    """A crash mid-write truncates the last line; everything durable before it must still verify."""
    led = _ledger(tmp_path)
    _fill(led, 3)
    path = next((tmp_path / "ledger").glob("*.jsonl"))
    with path.open("a", encoding="utf-8") as f:
        f.write('{"seq": 3, "event": "FIL')                # torn
    rows = read_stream(path)
    assert len(rows) == 3
    assert verify_chain(rows) == (True, "ok")


def test_a_failed_write_does_not_advance_the_sequence(tmp_path: Path, monkeypatch) -> None:
    """A phantom gap would make a healthy stream look tampered with."""
    led = _ledger(tmp_path)
    led.append("RISK_CHECK", {"i": 0})

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(led, "_write", _boom)
    assert led.append("RISK_CHECK", {"i": 1}) is None     # non-strict: logged, not raised
    monkeypatch.undo()
    led.append("RISK_CHECK", {"i": 2})
    assert [r["seq"] for r in led.rows()] == [0, 1]       # slot reused, no gap
    assert led.verify() == (True, "ok")


def test_strict_mode_raises_instead_of_losing_a_row(tmp_path: Path, monkeypatch) -> None:
    """The deployment that would rather stop trading than trade unrecorded."""
    led = _ledger(tmp_path, strict=True)

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(led, "_write", _boom)
    with pytest.raises(OSError, match="disk full"):
        led.append("RISK_CHECK", {"i": 0})


# --- router dual-write -----------------------------------------------------------------------

def test_router_dual_writes_the_order_path(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    broker = _StubBroker()
    router = Router.build_default(mode="paper", broker=broker, state_dir=tmp_path, ledger=led)
    intent = _intent()
    router.submit(intent, equity=100_000.0, existing_risk=0.0, open_positions=0)

    events = [r["event"] for r in led.rows()]
    assert events == ["RISK_CHECK", "BROKER_SUBMITTED", "FILLED"]
    assert {r["intent_id"] for r in led.rows()} == {intent.intent_id}   # one identity throughout
    filled = led.rows()[-1]
    assert filled["broker_order_id"] == broker.placed[0].id
    assert filled["execution"]["quantity"] == intent.shares
    assert led.verify() == (True, "ok")
    # The old journals are untouched by the ledger — dual-write, not replacement.
    assert (tmp_path / "orders.jsonl").exists() and (tmp_path / "fills.jsonl").exists()


def test_router_records_a_rejection_with_its_reasons(tmp_path: Path) -> None:
    led = _ledger(tmp_path)
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                  ledger=led, min_ticket_usd=1_000_000.0)
    router.submit(_intent(), equity=100_000.0, existing_risk=0.0, open_positions=0)
    rows = led.rows()
    assert [r["event"] for r in rows] == ["RISK_REJECTED"]
    assert rows[0]["payload"]["rejected_reasons"]
    assert rows[0]["payload"]["accepted"] is False


def test_a_router_without_a_ledger_behaves_exactly_as_before(tmp_path: Path) -> None:
    """Dual-write must be opt-in: every existing caller passes no ledger."""
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path)
    assert router.ledger is None
    order = router.submit(_intent(), equity=100_000.0, existing_risk=0.0, open_positions=0)
    assert order is not None
    assert not (tmp_path / "ledger").exists()
    assert json.loads((tmp_path / "orders.jsonl").read_text(encoding="utf-8").splitlines()[0])


def test_a_broken_ledger_never_breaks_a_trade(tmp_path: Path, monkeypatch) -> None:
    """Non-strict is the default precisely so an audit failure cannot halt a live book."""
    led = _ledger(tmp_path)
    monkeypatch.setattr(led, "_write", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path,
                                  ledger=led)
    order = router.submit(_intent(), equity=100_000.0, existing_risk=0.0, open_positions=0)
    assert order is not None                              # the trade still happened
    assert led.rows() == []                               # and the loss of the record is visible
