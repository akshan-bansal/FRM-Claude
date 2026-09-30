"""One parameter-precedence chain for building a live strategy instance.

Until 2026-09-18 the QT paper path built every strategy zero-arg (``STRATEGIES[name]()``), so it ran
class defaults while the entry alerts rendered the walk-forward registry's params: the alerts
described a config that never traded (NEXT_SESSION: "The live paper path runs STOCK CLASS
DEFAULTS"). This module makes the choice explicit and observable.

Modes:

* ``"default"`` — class defaults (today's behaviour; the CLI default until the user flips it).
* ``"wf"`` — the walk-forward registry's params for this symbol, **only if** the registry entry is
  for the same strategy; otherwise fall through to calibrated, then defaults.
* ``"calibrated"`` — ``analysis.calibration.calibrated_kwargs`` (per-asset-class heuristics and
  the 2026-09-15 sweep overrides), then defaults.

A parameter set the constructor rejects never breaks a launch: it falls back to defaults and says
so in ``ResolvedStrategy.note``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from trading_live_claude.logging_setup import get_logger

log = get_logger(__name__)

ParamsMode = Literal["default", "wf", "calibrated"]
PARAMS_MODES: tuple[str, ...] = ("default", "wf", "calibrated")


@dataclass
class ResolvedStrategy:
    strategy: object                   # the constructed Strategy instance
    source: str                        # "wf" | "calibrated" | "default"
    requested: dict[str, object] = field(default_factory=dict)   # kwargs that were passed
    note: str = ""                     # why a requested source was not used, if it wasn't


def _wf_params(strategy_name: str, symbol: str) -> dict[str, object] | None:
    from trading_live_claude.analysis.universe import WALK_FORWARD_VALIDATED
    rec = WALK_FORWARD_VALIDATED.get(symbol.upper())
    if rec is None or rec.strategy != strategy_name:
        return None
    return dict(rec.params or {})


def _calibrated_params(strategy_name: str, symbol: str) -> dict[str, object]:
    from trading_live_claude.analysis.calibration import calibrated_kwargs
    return dict(calibrated_kwargs(strategy_name, symbol))


def resolve_params(strategy_name: str, symbol: str, mode: str) -> tuple[dict[str, object], str, str]:
    """``(kwargs, source, note)`` for ``strategy_name`` on ``symbol`` under ``mode``.

    Precedence for ``wf``: registry (same strategy) -> calibrated -> defaults. For ``calibrated``:
    calibrated -> defaults. ``default`` is always ``({}, "default", "")``.
    """
    if mode not in PARAMS_MODES:
        raise ValueError(f"params mode must be one of {PARAMS_MODES}, got {mode!r}")
    if mode == "default":
        return {}, "default", ""
    note = ""
    if mode == "wf":
        wf = _wf_params(strategy_name, symbol)
        if wf is not None:
            return wf, "wf", ""
        note = "no registry entry for this strategy on this symbol"
    cal = _calibrated_params(strategy_name, symbol)
    if cal:
        return cal, "calibrated", note
    return {}, "default", (note + "; " if note else "") + "no calibrator"


def build_strategy(strategy_name: str, symbol: str, mode: str = "default") -> ResolvedStrategy:
    """Construct ``strategy_name`` for ``symbol`` with params from ``mode`` (never raises on params)."""
    from trading_live_claude.strategies import STRATEGIES

    cls = STRATEGIES[strategy_name]
    kwargs, source, note = resolve_params(strategy_name, symbol, mode)
    if not kwargs:
        return ResolvedStrategy(cls(), "default", {}, note)
    try:
        return ResolvedStrategy(cls(**kwargs), source, kwargs, note)
    except TypeError as e:
        log.warning("params.rejected_by_constructor", strategy=strategy_name, symbol=symbol,
                    source=source, kwargs=kwargs, error=str(e))
        return ResolvedStrategy(cls(), "default", kwargs,
                                f"{source} params rejected by the constructor ({e}); using defaults")


def running_params(strategy: object) -> dict[str, object]:
    """The params an instance actually runs with (``Strategy.params``), for alerts and journals."""
    p = getattr(strategy, "params", None)
    return dict(p) if isinstance(p, dict) else {}
