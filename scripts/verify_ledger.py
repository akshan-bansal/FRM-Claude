"""Verify the audit ledger's hash chains and reconstruct an intent's history.

Read-only. Two jobs, both from ``AUDIT_LEDGER_SCOPE.md`` section 2.5:

* ``--stream NAME`` (or no argument for every stream): recompute each chain and report the first
  break. This is the tamper check — it answers "has this record been edited since it was written".
* ``--intent ID``: print every event recorded for one intent, in order, across streams. This is the
  reconstruction check — "show me everything that happened to this order".

Exit status is 0 only when every stream verifies, so it is usable from a scheduled check.

    python scripts/verify_ledger.py
    python scripts/verify_ledger.py --stream kraken
    python scripts/verify_ledger.py --intent 18d848e7c6b04c84-d3beda056eab
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
    except Exception:                                                  # pragma: no cover
        pass

from trading_live_claude.audit import read_stream, verify_chain
from trading_live_claude.config import get_settings


def _streams(ledger_dir: Path) -> dict[str, list[Path]]:
    """``{stream: [day files, oldest first]}``, parsed from ``<date>.<stream>.jsonl``."""
    out: dict[str, list[Path]] = {}
    for path in sorted(ledger_dir.glob("*.jsonl")):
        parts = path.name.split(".")
        if len(parts) < 3:
            continue
        out.setdefault(".".join(parts[1:-1]), []).append(path)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger-dir", type=Path, default=None,
                    help="Defaults to <state_dir>/ledger from settings.")
    ap.add_argument("--stream", default="", help="Verify one stream instead of all of them.")
    ap.add_argument("--intent", default="", help="Print the full history of one intent id.")
    args = ap.parse_args()

    ledger_dir = args.ledger_dir or (Path(get_settings().state_dir) / "ledger")
    if not ledger_dir.exists():
        print(f"[verify] no ledger at {ledger_dir} — nothing has been recorded yet.")
        return 0

    streams = _streams(ledger_dir)
    if args.stream:
        streams = {k: v for k, v in streams.items() if k == args.stream}
        if not streams:
            print(f"[verify] no stream named {args.stream!r} in {ledger_dir}")
            return 1
    if not streams:
        print(f"[verify] {ledger_dir} holds no ledger files.")
        return 0

    if args.intent:
        found: list[tuple[str, dict]] = []
        for name, paths in streams.items():
            for path in paths:
                found += [(name, r) for r in read_stream(path)
                          if r.get("intent_id") == args.intent]
        if not found:
            print(f"[verify] no events for intent {args.intent}")
            return 1
        print(f"[verify] {len(found)} event(s) for intent {args.intent}:")
        for name, row in found:
            exe = row.get("execution") or {}
            extra = f"  order={row.get('broker_order_id')}" if row.get("broker_order_id") else ""
            price = f"  price={exe.get('price')}" if exe.get("price") is not None else ""
            print(f"  {row.get('ts')}  {name:>8}  seq={row.get('seq'):<5} "
                  f"{row.get('event')!s:<16}{extra}{price}")
        # Versions: which code and which thresholds produced these rows (audit phase 4). Printed
        # once rather than per row, with every distinct value, so a mid-session change is visible.
        for label, key in (("strategy", "strategy_id"), ("strategy_version", "strategy_version"),
                           ("risk_check_version", "risk_check_version")):
            seen = sorted({str(r.get(key)) for _n, r in found if r.get(key)})
            if seen:
                print(f"  {label:>18}: {', '.join(seen)}")
        return 0

    failed = 0
    for name, paths in sorted(streams.items()):
        rows: list[dict] = []
        for path in paths:
            rows += read_stream(path)
        ok, reason = verify_chain(rows)
        status = "OK  " if ok else "FAIL"
        span = f"{rows[0].get('ts', '?')[:19]} -> {rows[-1].get('ts', '?')[:19]}" if rows else "empty"
        print(f"[verify] {status} {name:>10}  {len(rows):>6} rows  {len(paths)} file(s)  {span}")
        if not ok:
            print(f"         {reason}")
            failed += 1
    if failed:
        print(f"[verify] {failed} stream(s) FAILED verification — the record has been altered.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
