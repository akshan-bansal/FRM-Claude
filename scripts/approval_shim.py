"""Thin CLI wrapper around :mod:`trading_live_claude.execution.approval_server`.

Runs the approval shim as a standalone process — useful when you want the
card facing an always-on machine rather than a laptop-side daemon spawned
from ``paper_ib.py`` / ``paper_kraken.py`` via ``--require-card``.

The real logic lives in the module; this file is only argparse + a call to
``run_shim`` so ``scripts/`` never becomes an import target for anything
inside ``src/``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trading_live_claude.execution.approval import (
    CardRegistry,
    InMemoryApprovalStore,
)
from trading_live_claude.execution.approval_server import BookRef, run_shim


def main() -> None:
    ap = argparse.ArgumentParser(description="TradeCard approval shim")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--auth-token", default=None,
                    help="Require this bearer token on every non-/healthz request. "
                         "Omit to run OPEN (only safe on strict loopback).")
    ap.add_argument("--state-dir", type=Path, default=None,
                    help="Journal directory the desk panel reads equity and meter readings from. "
                         "Without it (and --session-id) those figures are reported as unread "
                         "rather than guessed.")
    ap.add_argument("--session-id", default=None,
                    help="The book this shim reports on — the session_id a paper script prints at "
                         "boot, and the one its rows carry in state/paper_equity.csv.")
    ap.add_argument("--db", type=Path, default=None,
                    help="SQLite approval store to share with the paper loops (state/approval.db). "
                         "Without it the shim runs an in-memory store and CANNOT see prompts the "
                         "loops wrote, which is why a standalone shim used to look empty.")
    ap.add_argument("--book", action="append", default=[], metavar="VENUE:SESSION[:CCY[:CLASS]]",
                    help="A book this shim reports on, repeatable: one port, several brokers "
                         "(e.g. --book questrade:af9d28bc..:CAD:equity --book kraken:d53afb8f..:USD:crypto). "
                         "Books are never summed across venues or currencies; see GET /v1/books.")
    ap.add_argument("--currency", default="USD",
                    help="Account currency the journal's figures are denominated in.")
    args = ap.parse_args()
    if bool(args.state_dir) ^ bool(args.session_id):
        ap.error("--state-dir and --session-id go together: a journal without a session id says "
                 "nothing about which book, and a session id without a journal has nothing to read")
    books: list[BookRef] = []
    for spec in args.book:
        parts = spec.split(":")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            ap.error(f"--book wants VENUE:SESSION[:CCY[:CLASS]], got {spec!r}")
        books.append(BookRef(venue=parts[0], session_id=parts[1],
                             currency=parts[2] if len(parts) > 2 and parts[2] else args.currency,
                             asset_class=parts[3] if len(parts) > 3 else ""))
    if args.db is not None:
        # Share the loops' store, or this shim reports on a book it cannot see.
        from trading_live_claude.execution.approval_sqlite import (
            SqliteApprovalStore,
            SqliteCardRegistry,
        )
        registry = SqliteCardRegistry(args.db)
        store = SqliteApprovalStore(registry, args.db)
    else:
        registry = CardRegistry()
        store = InMemoryApprovalStore(registry)
    run_shim(store, registry, host=args.host, port=args.port,
             auth_token=args.auth_token, state_dir=args.state_dir,
             session_id=args.session_id, account_currency=args.currency, books=books or None)


if __name__ == "__main__":
    main()
