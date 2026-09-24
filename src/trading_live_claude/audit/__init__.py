"""Audit ledger: tamper-evident, event-sourced record of every trading state transition.

See ``AUDIT_LEDGER_SCOPE.md`` for the full design. Phase 3 (this package) is the writer, the
envelope and the hash chain; versioning, full event coverage and anchoring come later.
"""
from .ledger import (
    GENESIS,
    LEDGER_EVENTS,
    ChainBreak,
    Ledger,
    LedgerEvent,
    canonical_json,
    read_stream,
    row_hash,
    verify_chain,
)
from .versioning import risk_check_version, strategy_version

__all__ = [
    "GENESIS",
    "LEDGER_EVENTS",
    "ChainBreak",
    "Ledger",
    "LedgerEvent",
    "canonical_json",
    "read_stream",
    "risk_check_version",
    "row_hash",
    "strategy_version",
    "verify_chain",
]
