"""Content-addressed versions for strategies and the risk gate (``AUDIT_LEDGER_SCOPE.md`` phase 4).

A ledger row says *what* happened; these say *which code and which settings* made it happen. Without
them, a row from last month is uninterpretable: the strategy may have been retuned and a gate
threshold moved, and nothing in the record would show it.

Both versions are content hashes, not hand-maintained numbers, because a hand-maintained version is
one someone forgets to bump. A strategy's version changes when its source or its parameters change;
the gate's version changes when the gate code or any threshold changes.

**Known limit, stated rather than hidden:** ``strategy_version`` hashes the strategy module's own
source plus the instance's parameters. It does NOT follow imports, so an edit to a shared helper
(``signals/indicators.py``, say) changes behaviour without changing the version. Closing that would
mean hashing the transitive import graph — deferred deliberately; if it matters later, hash the
installed package tree instead of guessing which imports are relevant.
"""
from __future__ import annotations

import inspect
from hashlib import sha256
from typing import Any

from ..logging_setup import get_logger
from .ledger import canonical_json

log = get_logger(__name__)

# 16 hex chars = 64 bits. Enough that an accidental collision between two versions of one
# strategy is not a practical concern, short enough to read in a log line.
_DIGEST_CHARS = 16

UNKNOWN = "unknown"


def content_hash(payload: Any) -> str:
    """Stable short digest of any JSON-serialisable content."""
    return sha256(canonical_json(payload)).hexdigest()[:_DIGEST_CHARS]


def module_source(obj: object) -> str:
    """Source of the module defining ``obj``'s type (or ``obj`` itself for a class/function).

    Returns ``""`` when the source is unavailable — a C extension, an interactive session, or a
    frozen build. Callers must treat that as "unversioned", never as "unchanged".
    """
    target = obj if inspect.isclass(obj) or inspect.isfunction(obj) else type(obj)
    try:
        module = inspect.getmodule(target)
        return inspect.getsource(module) if module is not None else ""
    except (OSError, TypeError):
        return ""


def strategy_params(strategy: object) -> dict[str, Any]:
    """The parameters that define this instance's behaviour.

    ``Strategy.__init__`` stores its kwargs on ``self.params``; the per-trade exit knobs are class
    attributes an instance can override, and they change behaviour, so they belong in the version.
    """
    params = dict(getattr(strategy, "params", {}) or {})
    for knob in ("stop_atr_mult", "trail_atr_mult", "time_stop_bars", "supports_level_trigger"):
        if hasattr(strategy, knob):
            params[knob] = getattr(strategy, knob)
    return params


def strategy_version(strategy: object) -> str:
    """Version of one strategy *instance*: its module source plus its parameters.

    Two instances of the same class with different parameters version differently — that is the
    point, since ``bollinger(n_std=2.0)`` and ``bollinger(n_std=2.5)`` are different strategies as
    far as a later reader is concerned.
    """
    source = module_source(strategy)
    if not source:
        log.warning("versioning.source_unavailable", strategy=getattr(strategy, "name", strategy))
        return UNKNOWN
    return content_hash({
        "class": type(strategy).__qualname__,
        "source": source,
        "params": strategy_params(strategy),
    })


def gate_thresholds(router: object) -> dict[str, Any]:
    """Every number the risk gate compares against, flattened for hashing.

    Read off the live router rather than from settings, so the version reflects what this process
    actually enforces — not what a config file said at some point.
    """
    heat = getattr(router, "heat", None)
    ks = getattr(router, "kill_switch", None)
    budget = getattr(router, "daily_budget", None)
    out: dict[str, Any] = {
        "mode": getattr(router, "mode", None),
        "max_open_positions": getattr(router, "max_open_positions", None),
        "min_ticket_usd": getattr(router, "min_ticket_usd", None),
        "max_gross_leverage": getattr(router, "max_gross_leverage", None),
        "max_position_notional_pct": getattr(router, "max_position_notional_pct", None),
        "force_exit_atr_mult": getattr(router, "force_exit_atr_mult", None),
        "on_size_cap_breach": getattr(router, "on_size_cap_breach", None),
        "heat_cap_pct": getattr(heat, "cap_pct", None),
        "max_drawdown_pct": getattr(ks, "max_drawdown_pct", None),
        "daily_loss_limit_pct": getattr(ks, "daily_loss_limit_pct", None),
    }
    if budget is not None:
        out["daily_max_trades"] = getattr(budget, "max_trades", None)
        out["daily_max_notional_usd"] = getattr(budget, "max_notional", None)
    return out


def risk_check_version(router: object) -> str:
    """Version of the risk check: the gate's own source plus every threshold it enforces.

    Hashing the source matters as much as the thresholds: adding, removing or reordering a gate
    changes what "RISK_CHECK accepted" meant, with every threshold untouched.
    """
    gate = getattr(type(router), "_gate", None)
    try:
        source = inspect.getsource(gate) if gate is not None else ""
    except (OSError, TypeError):
        source = ""
    if not source:
        log.warning("versioning.gate_source_unavailable", router=type(router).__qualname__)
        return UNKNOWN
    return content_hash({"gate_source": source, "thresholds": gate_thresholds(router)})
