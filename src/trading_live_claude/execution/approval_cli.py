"""Standalone CLI entrypoint for the TradeCard approval shim.

Installed as ``tradecard-shim`` when the package is installed with the
``[shim]`` extras group (``pip install trading-live-claude[shim]``).

Backs the systemd unit / Dockerfile under ``deploy/``; also the thing a
human runs by hand for a quick standalone shim outside the paper
scripts. Not the same as ``scripts/approval_shim.py``, which stays around
as the source-tree convenience wrapper.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from ..intel.vs_engine import DEFAULT_WRITEUP_DIR
from .approval import CardRegistry, InMemoryApprovalStore
from .approval_server import run_shim


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="tradecard-shim",
        description="TradeCard approval shim — REST wire between the trading "
                    "engine's ApprovalRouter and a physical Ed25519 card.",
    )
    ap.add_argument("--host", default="127.0.0.1",
                    help="Bind address. Default: loopback only. Use 0.0.0.0 "
                         "only behind a trusted tunnel (Tailscale / Cloudflare).")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--auth-token", default=None,
                    help="Require this bearer token on every non-/healthz "
                         "request. Omit for OPEN mode (loopback only).")
    ap.add_argument("--db", type=Path, default=None,
                    help="Path to a SQLite database for persistent store + "
                         "registry. When omitted, the shim runs with an "
                         "in-memory store that resets every restart.")
    ap.add_argument("--writeup-dir", type=Path, default=None,
                    help=f"VS-engine writeup directory served by "
                         f"GET /v1/intel/{{ref}}. Default: {DEFAULT_WRITEUP_DIR}")
    return ap


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)

    if args.db is not None:
        # Local import so the module stays importable without SQLite deps
        # in contexts that only care about the in-memory path.
        from .approval_sqlite import SqliteApprovalStore, SqliteCardRegistry
        registry = SqliteCardRegistry(args.db)
        store = SqliteApprovalStore(registry, args.db)
    else:
        registry = CardRegistry()
        store = InMemoryApprovalStore(registry)

    run_shim(
        store, registry,
        host=args.host, port=args.port,
        writeup_dir=args.writeup_dir or DEFAULT_WRITEUP_DIR,
        auth_token=args.auth_token,
    )


if __name__ == "__main__":
    main()
