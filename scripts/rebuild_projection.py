"""Rebuild the SQLite read model from the audit ledger, then answer the scope's questions.

Read-only with respect to the ledger: it replays ``state/ledger/*.jsonl`` into
``state/audit_projection.db``, which is derived and disposable — delete it and rebuild whenever you
doubt it. The ledger stays the source of truth; if the two disagree, the ledger wins.

Runs in its own process, never inside a poll loop (walking every ledger file does a lot of small
writes). Exit status is 1 if any stream's hash chain fails verification, so this is usable as a
scheduled integrity check.

    python scripts/rebuild_projection.py
    python scripts/rebuild_projection.py --symbol PAXG/USD
    python scripts/rebuild_projection.py --db /tmp/scratch.db --ledger-dir state/ledger
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
    except Exception:                                                  # pragma: no cover
        pass

from trading_live_claude.audit.projection import (
    approval_latency_seconds,
    dangling_submissions,
    open_db,
    rebuild,
    rejection_reasons,
    versions_traded,
)
from trading_live_claude.config import get_settings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger-dir", type=Path, default=None,
                    help="Defaults to <state_dir>/ledger.")
    ap.add_argument("--db", type=Path, default=None,
                    help="Projection database. Defaults to <state_dir>/audit_projection.db.")
    ap.add_argument("--symbol", default="", help="Also show which strategy versions traded it.")
    args = ap.parse_args()

    state = Path(get_settings().state_dir)
    ledger_dir = args.ledger_dir or (state / "ledger")
    db_path = args.db or (state / "audit_projection.db")

    if not ledger_dir.exists():
        print(f"[projection] no ledger at {ledger_dir} — nothing recorded yet.")
        return 0

    summary = rebuild(ledger_dir, db_path)
    print(f"[projection] {db_path}")
    print(f"[projection] {summary['events']} event(s), {summary['intents']} intent(s), "
          f"{len(summary['streams'])} stream(s)")
    for stream, s in sorted(summary["streams"].items()):
        status = "OK  " if s["ok"] else "FAIL"
        print(f"  {status} {stream:>12}  {s['rows']:>6} rows  {s['files']} file(s)"
              + ("" if s["ok"] else f"  — {s['reason']}"))

    conn = open_db(db_path)
    outcomes = list(conn.execute(
        "SELECT outcome, COUNT(*) FROM intents GROUP BY 1 ORDER BY 2 DESC"))
    if outcomes:
        print("[projection] outcomes: " + ", ".join(f"{o}={n}" for o, n in outcomes))

    reasons = rejection_reasons(conn)
    if reasons:
        print("[projection] top gate rejections:")
        for reason, n in reasons[:8]:
            print(f"    {n:>4}x {reason[:96]}")

    latency = approval_latency_seconds(conn)
    if latency:
        print(f"[projection] card response: n={len(latency)} "
              f"median={statistics.median(latency):.1f}s max={max(latency):.1f}s")

    dangling = dangling_submissions(conn)
    if dangling:
        print(f"[projection] {len(dangling)} submitted intent(s) with NO terminal event "
              f"(possible crashed writer — the order may have filled):")
        for intent_id, symbol, last in dangling[:10]:
            print(f"    {intent_id}  {symbol:<10} last seen {last}")

    if args.symbol:
        rows = versions_traded(conn, args.symbol.upper())
        print(f"[projection] versions that traded {args.symbol.upper()}:"
              + ("" if rows else " none"))
        for strategy, version, fills in rows:
            print(f"    {strategy:<18} {version:<18} {fills} fill(s)")
    conn.close()

    if not summary["chains_ok"]:
        print("[projection] a chain FAILED verification — the record has been altered.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
