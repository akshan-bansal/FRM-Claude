"""SQLite projection of the ledger (AUDIT_LEDGER_SCOPE.md phase 7).

The projection is derived and disposable, so the properties worth testing are: a rebuild is
idempotent, the fold answers the scope's questions, and a broken chain is reported rather than
quietly projected as if it were sound.
"""
from __future__ import annotations

import json
from pathlib import Path

from trading_live_claude.audit import Ledger
from trading_live_claude.audit.projection import (
    approval_latency_seconds,
    dangling_submissions,
    open_db,
    rebuild,
    rejection_reasons,
    signed_intents,
    streams,
    versions_traded,
)


def _seed(tmp_path: Path) -> Ledger:
    """One filled intent, one gate rejection, one approved+signed intent, one bare signal."""
    led = Ledger(tmp_path / "ledger", stream="kraken", session_id="s1")
    led.append("RISK_CHECK", {"accepted": True, "rejected_reasons": [], "symbol": "XIC.TO",
                              "action": "Buy", "shares": 10, "entry": 31.0},
               intent_id="i-fill", strategy_id="bollinger", strategy_version="v-boll",
               risk_check_version="v-gate")
    led.append("BROKER_SUBMITTED", {"symbol": "XIC.TO"}, intent_id="i-fill",
               risk_check_version="v-gate")
    led.append("FILLED", {"symbol": "XIC.TO", "shares": 10, "requested_shares": 10},
               intent_id="i-fill", broker_order_id=7, strategy_id="bollinger",
               strategy_version="v-boll", risk_check_version="v-gate")
    led.append("RISK_REJECTED", {"accepted": False, "symbol": "QQQ",
                                 "rejected_reasons": ["notional $1.00 below min $100"]},
               intent_id="i-rej", strategy_id="bollinger", risk_check_version="v-gate")
    led.append("INTENT_SENT", {"symbol": "PAXG/USD", "fingerprint": "AAAA...BBBB"},
               intent_id="i-card", at=None)
    led.append("APPROVED", {"symbol": "PAXG/USD", "verdict": "ACCEPT",
                            "signer_card_id": "card-1", "fingerprint": "AAAA...BBBB"},
               intent_id="i-card")
    led.append("SIGNED", {"symbol": "PAXG/USD", "canonical": "paper|Buy|PAXG/USD|2|…",
                          "fingerprint": "AAAA...BBBB"},
               intent_id="i-card", signature="c2ln", signing_key_id="card-1")
    led.append("SIGNAL_SUPPRESSED", {"symbol": "ZEC/USD", "reason": "overlay halt"},
               strategy_id="bollinger")          # no intent_id: a signal that never became one
    return led


def test_rebuild_populates_the_three_tables(tmp_path: Path) -> None:
    led = _seed(tmp_path)
    summary = rebuild(tmp_path / "ledger", tmp_path / "p.db")
    assert summary["events"] == len(led.rows()) == 8
    assert summary["intents"] == 3                  # the bare signal has no intent row
    assert summary["chains_ok"] is True

    conn = open_db(tmp_path / "p.db")
    assert conn.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0] == 8
    assert conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 3
    status = conn.execute("SELECT stream, rows, ok, reason FROM chain_status").fetchall()
    assert status == [("kraken", 8, 1, "ok")]


def test_an_event_without_an_intent_id_is_not_invented_into_an_intent(tmp_path: Path) -> None:
    """A suppressed signal is real history, but it is not an intent — fabricating one would lie."""
    _seed(tmp_path)
    rebuild(tmp_path / "ledger", tmp_path / "p.db")
    conn = open_db(tmp_path / "p.db")
    assert conn.execute("SELECT COUNT(*) FROM ledger_events WHERE event='SIGNAL_SUPPRESSED'"
                        ).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM intents WHERE intent_id IS NULL").fetchone()[0] == 0


def test_the_fold_summarises_each_intents_life(tmp_path: Path) -> None:
    _seed(tmp_path)
    rebuild(tmp_path / "ledger", tmp_path / "p.db")
    conn = open_db(tmp_path / "p.db")
    rows = {r[0]: r for r in conn.execute(
        "SELECT intent_id, symbol, outcome, terminal_event, filled_shares, broker_order_id, "
        "gate_accepted, strategy_version, risk_check_version, verdict, signer_card_id, "
        "fingerprint, event_count FROM intents")}

    fill = rows["i-fill"]
    assert fill[1] == "XIC.TO" and fill[2] == "filled" and fill[3] == "FILLED"
    assert fill[4] == 10 and fill[5] == "7" and fill[6] == 1
    assert fill[7] == "v-boll" and fill[8] == "v-gate"
    assert fill[12] == 3

    rej = rows["i-rej"]
    assert rej[2] == "rejected" and rej[6] == 0

    card = rows["i-card"]
    assert card[9] == "ACCEPT" and card[10] == "card-1" and card[11] == "AAAA...BBBB"


def test_rebuilding_twice_is_idempotent(tmp_path: Path) -> None:
    """The projection is disposable; a rebuild must not duplicate or drift."""
    _seed(tmp_path)
    first = rebuild(tmp_path / "ledger", tmp_path / "p.db")
    second = rebuild(tmp_path / "ledger", tmp_path / "p.db")
    assert first["events"] == second["events"]
    assert first["intents"] == second["intents"]
    conn = open_db(tmp_path / "p.db")
    assert conn.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0] == first["events"]


def test_new_events_appear_on_the_next_rebuild(tmp_path: Path) -> None:
    led = _seed(tmp_path)
    rebuild(tmp_path / "ledger", tmp_path / "p.db")
    led.append("FILLED", {"symbol": "SOL/USD", "shares": 5, "requested_shares": 5},
               intent_id="i-new", broker_order_id=9)
    summary = rebuild(tmp_path / "ledger", tmp_path / "p.db")
    assert summary["events"] == 9 and summary["intents"] == 4
    conn = open_db(tmp_path / "p.db")
    assert conn.execute("SELECT outcome FROM intents WHERE intent_id='i-new'").fetchone()[0] \
        == "filled"


def test_a_broken_chain_is_reported_not_silently_projected(tmp_path: Path) -> None:
    """Projecting a tampered ledger as if it were sound would launder the tampering."""
    _seed(tmp_path)
    path = next((tmp_path / "ledger").glob("*.jsonl"))
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    rows[1]["payload"]["symbol"] = "TSLA"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    summary = rebuild(tmp_path / "ledger", tmp_path / "p.db")
    assert summary["chains_ok"] is False
    conn = open_db(tmp_path / "p.db")
    ok, reason = conn.execute("SELECT ok, reason FROM chain_status").fetchone()
    assert ok == 0 and "payload of row 1" in reason


def test_partial_fills_and_trims_are_distinguishable(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "ledger", stream="qt", session_id="s1")
    led.append("RISK_TRIMMED", {"from_shares": 100, "to_shares": 40, "symbol": "VALE",
                                "reason": "symbol_cap=50.0%>20.0%"}, intent_id="i-trim")
    led.append("PARTIAL", {"symbol": "VALE", "shares": 25, "requested_shares": 40},
               intent_id="i-trim", broker_order_id=3)
    rebuild(tmp_path / "ledger", tmp_path / "p.db")
    conn = open_db(tmp_path / "p.db")
    row = conn.execute("SELECT trimmed_from, requested_shares, filled_shares, outcome "
                       "FROM intents WHERE intent_id='i-trim'").fetchone()
    assert row == (100.0, 40.0, 25.0, "partial")


def test_streams_are_projected_separately(tmp_path: Path) -> None:
    Ledger(tmp_path / "ledger", stream="qt").append("FILLED", {"symbol": "A"}, intent_id="a")
    Ledger(tmp_path / "ledger", stream="kraken").append("FILLED", {"symbol": "B"}, intent_id="b")
    assert set(streams(tmp_path / "ledger")) == {"qt", "kraken"}
    rebuild(tmp_path / "ledger", tmp_path / "p.db")
    conn = open_db(tmp_path / "p.db")
    assert conn.execute("SELECT COUNT(*) FROM chain_status").fetchone()[0] == 2
    assert {r[0] for r in conn.execute("SELECT stream FROM intents")} == {"qt", "kraken"}


# --- the scope's questions -------------------------------------------------------------------

def test_rejection_reasons_are_countable(tmp_path: Path) -> None:
    _seed(tmp_path)
    rebuild(tmp_path / "ledger", tmp_path / "p.db")
    reasons = rejection_reasons(open_db(tmp_path / "p.db"))
    assert reasons and reasons[0][0].startswith("notional")


def test_approval_latency_is_measurable(tmp_path: Path) -> None:
    """The TTL-pressure question: how long does the card actually take to answer?"""
    led = Ledger(tmp_path / "ledger", stream="k", session_id="s1")
    from datetime import UTC, datetime, timedelta
    t0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    led.append("INTENT_SENT", {"symbol": "X"}, intent_id="i1", at=t0)
    led.append("APPROVED", {"symbol": "X", "verdict": "ACCEPT"}, intent_id="i1",
               at=t0 + timedelta(seconds=14))
    rebuild(tmp_path / "ledger", tmp_path / "p.db")
    assert approval_latency_seconds(open_db(tmp_path / "p.db")) == [14.0]


def test_versions_traded_answers_which_code_traded_a_symbol(tmp_path: Path) -> None:
    _seed(tmp_path)
    rebuild(tmp_path / "ledger", tmp_path / "p.db")
    rows = versions_traded(open_db(tmp_path / "p.db"), "XIC.TO")
    assert rows == [("bollinger", "v-boll", 1)]


def test_a_submitted_intent_with_no_terminal_event_is_surfaced(tmp_path: Path) -> None:
    """The crashed-writer case: the order may have filled and the record simply stops."""
    led = Ledger(tmp_path / "ledger", stream="k", session_id="s1")
    led.append("RISK_CHECK", {"accepted": True, "symbol": "ETH/USD"}, intent_id="i-lost")
    led.append("BROKER_SUBMITTED", {"symbol": "ETH/USD"}, intent_id="i-lost")
    rebuild(tmp_path / "ledger", tmp_path / "p.db")
    dangling = dangling_submissions(open_db(tmp_path / "p.db"))
    assert [d[0] for d in dangling] == ["i-lost"]


def test_signed_intents_expose_what_is_needed_to_reverify(tmp_path: Path) -> None:
    _seed(tmp_path)
    rebuild(tmp_path / "ledger", tmp_path / "p.db")
    rows = signed_intents(open_db(tmp_path / "p.db"))
    assert len(rows) == 1
    intent_id, key_id, signature, canonical = rows[0]
    assert intent_id == "i-card" and key_id == "card-1" and signature == "c2ln"
    assert canonical.startswith("paper|Buy|PAXG/USD")


def test_an_empty_ledger_directory_projects_cleanly(tmp_path: Path) -> None:
    (tmp_path / "ledger").mkdir()
    summary = rebuild(tmp_path / "ledger", tmp_path / "p.db")
    assert summary == {"streams": {}, "events": 0, "intents": 0, "chains_ok": True}


# --- reconciliation against the legacy journals ----------------------------------------------

def _rl():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_ledger.py"
    spec = importlib.util.spec_from_file_location("reconcile_ledger", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _journal(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_matching_journal_and_ledger_reconcile_clean(tmp_path: Path) -> None:
    rl = _rl()
    _seed(tmp_path)
    rebuild(tmp_path / "ledger", tmp_path / "p.db")
    _journal(tmp_path / "fills.jsonl", [{"intent_id": "i-fill", "order_id": 7, "symbol": "XIC.TO"}])
    _journal(tmp_path / "rejected.jsonl", [{"intent_id": "i-rej", "symbol": "QQQ"}])
    findings = rl.reconcile(tmp_path, tmp_path / "p.db")
    assert findings["fills.jsonl"]["missing_in_ledger"] == []
    assert findings["rejected.jsonl"]["missing_in_ledger"] == []


def test_a_journal_fill_with_no_ledger_row_is_a_finding(tmp_path: Path) -> None:
    """The direction that matters: a trade happened and the audit record does not have it."""
    rl = _rl()
    _seed(tmp_path)
    rebuild(tmp_path / "ledger", tmp_path / "p.db")
    _journal(tmp_path / "fills.jsonl", [
        {"intent_id": "i-fill", "order_id": 7, "symbol": "XIC.TO"},
        {"intent_id": "i-ghost", "order_id": 99, "symbol": "SOL/USD"},
    ])
    findings = rl.reconcile(tmp_path, tmp_path / "p.db")
    assert findings["fills.jsonl"]["missing_in_ledger"] == [("i-ghost", "99")]


def test_pre_intent_id_journal_rows_are_classed_not_counted_as_mismatches(tmp_path: Path) -> None:
    """Rows written before intent ids existed have nothing to join on — that is not a mismatch."""
    rl = _rl()
    _seed(tmp_path)
    rebuild(tmp_path / "ledger", tmp_path / "p.db")
    _journal(tmp_path / "fills.jsonl", [
        {"order_id": 3, "symbol": "EQB.TO"},                 # old shape: no intent_id
        {"intent_id": "i-fill", "order_id": 7, "symbol": "XIC.TO"},
    ])
    findings = rl.reconcile(tmp_path, tmp_path / "p.db")
    assert findings["fills.jsonl"]["missing_in_ledger"] == []
    assert findings["fills.jsonl"]["unjoinable_no_intent_id"] == [("", "3")]


def test_since_ignores_journal_rows_older_than_the_ledger(tmp_path: Path) -> None:
    """Journals predate the ledger entirely, so --since is how a run becomes meaningful."""
    rl = _rl()
    _seed(tmp_path)
    rebuild(tmp_path / "ledger", tmp_path / "p.db")
    _journal(tmp_path / "fills.jsonl", [
        {"ts": "2026-09-01T00:00:00+00:00", "intent_id": "i-old", "order_id": 1},
        {"ts": "2026-09-24T12:00:00+00:00", "intent_id": "i-fill", "order_id": 7},
    ])
    assert rl.reconcile(tmp_path, tmp_path / "p.db")["fills.jsonl"]["missing_in_ledger"] \
        == [("i-old", "1")]
    scoped = rl.reconcile(tmp_path, tmp_path / "p.db", since="2026-09-24")
    assert scoped["fills.jsonl"]["missing_in_ledger"] == []


def test_ledger_only_rows_do_not_fail_the_run(tmp_path: Path) -> None:
    """The ledger records branches the journals never had; that is progress, not a discrepancy."""
    rl = _rl()
    _seed(tmp_path)
    rebuild(tmp_path / "ledger", tmp_path / "p.db")
    _journal(tmp_path / "fills.jsonl", [])
    findings = rl.reconcile(tmp_path, tmp_path / "p.db")
    assert findings["fills.jsonl"]["only_in_ledger"] == [("i-fill", "7")]
    assert findings["fills.jsonl"]["missing_in_ledger"] == []


def test_approval_db_verdicts_are_reconciled(tmp_path: Path) -> None:
    import sqlite3

    rl = _rl()
    _seed(tmp_path)
    rebuild(tmp_path / "ledger", tmp_path / "p.db")
    con = sqlite3.connect(tmp_path / "approval.db")
    con.executescript("CREATE TABLE intents (intent_id TEXT, verdict TEXT, resolved_at TEXT);")
    con.execute("INSERT INTO intents VALUES ('i-card','ACCEPT','2026-09-24T12:00:00+00:00')")
    con.execute("INSERT INTO intents VALUES ('i-absent','DECLINE','2026-09-24T12:00:00+00:00')")
    con.commit()
    con.close()
    findings = rl.reconcile(tmp_path, tmp_path / "p.db")
    assert findings["approval.db"]["missing_in_ledger"] == [("i-absent", "DECLINE")]


# --- runner wiring ---------------------------------------------------------------------------

def test_the_qt_signal_command_exposes_the_audit_ledger_flag() -> None:
    """Guards the wiring: if the flag disappears, the QT book silently stops recording."""
    from typer.testing import CliRunner

    from trading_live_claude.cli import app

    out = CliRunner().invoke(app, ["signal", "--help"]).output
    assert "--audit-ledger" in out and "--no-audit-ledger" in out
    assert "state/ledger" in out.replace("\n", "")


def test_both_paper_runners_default_the_ledger_on() -> None:
    """Kraken and QT must record by default; opting out has to be explicit."""
    import inspect

    from trading_live_claude import cli

    sig = inspect.signature(cli.signal)
    assert sig.parameters["audit_ledger"].default.default is True

    kraken = Path(__file__).resolve().parents[1] / "scripts" / "paper_kraken.py"
    src = kraken.read_text(encoding="utf-8")
    assert '"--audit-ledger"' in src and "default=True" in src


def test_the_two_books_use_different_streams() -> None:
    """One shared chain across processes would interleave; separate streams is the whole design."""
    root = Path(__file__).resolve().parents[1]
    qt = (root / "src" / "trading_live_claude" / "cli.py").read_text(encoding="utf-8")
    kraken = (root / "scripts" / "paper_kraken.py").read_text(encoding="utf-8")
    assert 'stream="qt"' in qt
    assert 'stream="kraken"' in kraken
