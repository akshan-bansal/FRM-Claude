"""Reconcile the audit ledger against the journals it was built to eventually replace.

Read-only. Compares, per source:

* ``state/fills.jsonl``   vs ledger FILLED / PARTIAL rows, joined on intent_id
* ``state/rejected.jsonl`` vs ledger RISK_REJECTED / BROKER_REJECTED rows
* ``state/approval.db``   vs ledger APPROVED / REJECTED / EXPIRED verdicts

Exit status is 1 when anything is missing on the ledger side, because that is the direction that
matters: a journal row with no ledger counterpart means the audit record is incomplete.

**Expect real mismatches on the first runs, and read them as findings rather than bugs.** Journals
predate the ledger entirely (anything before 2026-09-24), and only `paper_kraken.py` passes
``ledger=`` today, so Questrade and IB sessions produce journal rows with no ledger side until they
are wired the same way. Rows the ledger has and the journals do not are listed separately and do NOT
fail the run: the ledger records branches the journals never had (a suppressed signal, a queued
intent, a card verdict).

    python scripts/reconcile_ledger.py
    python scripts/reconcile_ledger.py --since 2026-09-24
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
    except Exception:                                                  # pragma: no cover
        pass

from trading_live_claude.audit.projection import open_db, rebuild
from trading_live_claude.config import get_settings


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _after(row: dict, since: str) -> bool:
    return not since or str(row.get("ts", "")) >= since


def reconcile(state: Path, db: Path, *, since: str = "") -> dict[str, dict[str, list]]:
    """Returns ``{source: {"missing_in_ledger": [...], "only_in_ledger": [...]}}``."""
    conn = open_db(db)
    findings: dict[str, dict[str, list]] = {}

    # --- fills ---------------------------------------------------------------------------- #
    ledger_fills = {
        (r[0], str(r[1])) for r in conn.execute(
            "SELECT intent_id, COALESCE(broker_order_id,'') FROM ledger_events "
            "WHERE event IN ('FILLED','PARTIAL') AND intent_id IS NOT NULL")
    }
    journal_fills = {(str(r.get("intent_id") or ""), str(r.get("order_id") or ""))
                     for r in _jsonl(state / "fills.jsonl") if _after(r, since)}
    # A journal row written before intent ids existed has no key to join on; report it as its own
    # class rather than as a mismatch, since there is nothing it could match.
    unkeyed = sorted(k for k in journal_fills if not k[0])
    keyed = {k for k in journal_fills if k[0]}
    findings["fills.jsonl"] = {
        "missing_in_ledger": sorted(keyed - ledger_fills),
        "only_in_ledger": sorted(ledger_fills - keyed),
        "unjoinable_no_intent_id": unkeyed,
    }

    # --- rejections ----------------------------------------------------------------------- #
    ledger_rejects = {
        r[0] for r in conn.execute(
            "SELECT intent_id FROM ledger_events "
            "WHERE event IN ('RISK_REJECTED','BROKER_REJECTED') AND intent_id IS NOT NULL")
    }
    journal_rejects = {str(r.get("intent_id") or "") for r in _jsonl(state / "rejected.jsonl")
                       if _after(r, since)}
    findings["rejected.jsonl"] = {
        "missing_in_ledger": sorted({k for k in journal_rejects if k} - ledger_rejects),
        "only_in_ledger": sorted(ledger_rejects - journal_rejects),
        "unjoinable_no_intent_id": ([f"<{sum(1 for k in journal_rejects if not k)} row(s)>"]
                                    if any(not k for k in journal_rejects) else []),
    }

    # --- approvals ------------------------------------------------------------------------ #
    approval_db = state / "approval.db"
    if approval_db.exists():
        ledger_verdicts = {
            (r[0], str(r[1])) for r in conn.execute(
                "SELECT intent_id, verdict FROM intents WHERE verdict IS NOT NULL")
        }
        acon = sqlite3.connect(f"file:{approval_db}?mode=ro", uri=True)
        store_verdicts = {
            (r[0], str(r[1])) for r in acon.execute(
                "SELECT intent_id, verdict, resolved_at FROM intents WHERE verdict IS NOT NULL")
            if not since or str(r[2] or "") >= since
        }
        acon.close()
        findings["approval.db"] = {
            "missing_in_ledger": sorted(store_verdicts - ledger_verdicts),
            "only_in_ledger": sorted(ledger_verdicts - store_verdicts),
        }
    conn.close()
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", type=Path, default=None)
    ap.add_argument("--db", type=Path, default=None, help="Projection db; rebuilt unless --no-rebuild.")
    ap.add_argument("--no-rebuild", action="store_true",
                    help="Reconcile against the projection as it stands.")
    ap.add_argument("--since", default="",
                    help="ISO date/prefix; ignore journal rows older than this. Journals predate "
                         "the ledger, so '--since 2026-09-24' is usually what you want.")
    args = ap.parse_args()

    state = args.state_dir or Path(get_settings().state_dir)
    db = args.db or (state / "audit_projection.db")
    if not (state / "ledger").exists():
        print(f"[reconcile] no ledger at {state / 'ledger'} — nothing to reconcile.")
        return 0
    if not args.no_rebuild:
        rebuild(state / "ledger", db)

    findings = reconcile(state, db, since=args.since)
    failed = 0
    for source, sets in findings.items():
        missing = sets.get("missing_in_ledger", [])
        extra = sets.get("only_in_ledger", [])
        unjoinable = sets.get("unjoinable_no_intent_id", [])
        status = "FAIL" if missing else "OK  "
        print(f"[reconcile] {status} {source:>16}  missing_in_ledger={len(missing)} "
              f"only_in_ledger={len(extra)}"
              + (f" unjoinable={len(unjoinable)}" if unjoinable else ""))
        for item in missing[:10]:
            print(f"         MISSING {item}")
        if unjoinable:
            print("         (unjoinable rows predate intent ids — expected for old journals)")
        if missing:
            failed += 1
    if failed:
        print(f"[reconcile] {failed} source(s) have journal rows with no ledger counterpart. "
              f"Before 2026-09-24 there was no ledger at all, and only paper_kraken.py passes "
              f"ledger= today — wire the QT and IB runners the same way, or pass --since.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
