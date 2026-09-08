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

from trading_live_claude.execution.approval import (  # noqa: E402
    CardRegistry,
    InMemoryApprovalStore,
)
from trading_live_claude.execution.approval_server import run_shim  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="TradeCard approval shim")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    registry = CardRegistry()
    store = InMemoryApprovalStore(registry)
    run_shim(store, registry, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
