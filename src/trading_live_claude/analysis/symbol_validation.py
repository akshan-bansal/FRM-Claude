"""Pre-flight symbol validation for paper/live monitor startup.

Both ARX.TO (2026-09-08) and RIG.TO (2026-09-09) landed in the QT paper monitor's
symbol list, resolved cleanly on ``symbols/search``, then returned HTTP 404 on every
subsequent ``markets/candles`` fetch — burning ~60 combined ``monitor.step.error`` rows
before manual intervention. This module runs a minimal fetch per symbol BEFORE the
monitor loop starts, so silent-listing quirks surface pre-launch instead of mid-run.

Not a symbol atlas (that's Gap 2 in the symbol-mapping architecture queue). Not a
data-quality check on the cache (that's a separate concern). Just: for each symbol,
does the broker's candle endpoint actually return bars?

Contract:

* :class:`SymbolValidation` — one row per checked symbol with status + reason.
* :func:`validate_sleeve` — walks a symbol list, returns a list of validations.
* :func:`refuse_launch_on_hard_failures` — helper the monitor scripts call to enforce
  the fail-closed policy: any HARD status raises SystemExit before the loop starts.

Status semantics:

* ``ok`` — candle fetch returned bars. Trades are viable.
* ``warn`` — candle fetch returned an empty list without raising. Symbol may be
  delisted or temporarily unlisted; monitor can still start but the strategy layer
  won't see history. Logged, does NOT refuse launch.
* ``fail`` — candle fetch raised. Almost always the 404-on-candles quirk seen with
  ARX.TO/RIG.TO. Monitor refuses to start (unless caller opts out).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from ..brokers.base import Broker
from ..logging_setup import get_logger

log = get_logger(__name__)


ValidationStatus = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class SymbolValidation:
    """Outcome of one pre-flight fetch attempt against a broker."""

    symbol: str
    status: ValidationStatus
    reason: str
    bars_fetched: int = 0
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def validate_sleeve(
    broker: Broker,
    symbols: list[str],
    *,
    lookback_days: int = 5,
    interval: str = "OneDay",
) -> list[SymbolValidation]:
    """For each symbol, attempt a small candle fetch and classify the result.

    ``lookback_days`` defaults to 5 — enough to cross a weekend and get at least one
    trading-day bar from a healthy listing. Shorter windows risk false-``warn``
    results on Mondays. Interval defaults to ``OneDay`` since every broker in this
    repo supports it; use the canonical IB/Kraken lexicon set as of the 2026-09-05
    interval standardization.

    Never raises. Broker exceptions become ``fail`` rows; empty candle lists become
    ``warn`` rows; non-empty responses are ``ok``.
    """
    end = datetime.now(UTC)
    start = end - timedelta(days=lookback_days)
    out: list[SymbolValidation] = []
    for sym in symbols:
        try:
            bars = broker.candles(sym, start, end, interval)
            if not bars:
                out.append(SymbolValidation(
                    symbol=sym, status="warn", bars_fetched=0,
                    reason=f"candle fetch returned empty over {lookback_days}d lookback",
                ))
            else:
                out.append(SymbolValidation(
                    symbol=sym, status="ok", bars_fetched=len(bars),
                    reason=f"{len(bars)} bar(s) returned",
                ))
        except Exception as e:                                 # noqa: BLE001 — every broker error is a validation fail
            # Compact the exception into the reason string. Preserve the type name so
            # ARX.TO/RIG.TO 404s are distinguishable from other failure modes (auth,
            # rate limit, malformed symbol, etc.) in the startup banner.
            out.append(SymbolValidation(
                symbol=sym, status="fail", bars_fetched=0,
                reason=f"{type(e).__name__}: {str(e)[:150]}",
            ))
    return out


def format_validation_banner(results: list[SymbolValidation]) -> str:
    """Human-readable summary for stdout at monitor startup."""
    ok = [r for r in results if r.status == "ok"]
    warn = [r for r in results if r.status == "warn"]
    fail = [r for r in results if r.status == "fail"]
    lines = [
        f"[symbol-validate] {len(results)} symbols checked: "
        f"{len(ok)} ok, {len(warn)} warn, {len(fail)} fail",
    ]
    for r in fail:
        lines.append(f"  FAIL  {r.symbol:>12}  {r.reason}")
    for r in warn:
        lines.append(f"  WARN  {r.symbol:>12}  {r.reason}")
    return "\n".join(lines)


def refuse_launch_on_hard_failures(
    results: list[SymbolValidation],
    *,
    allow_fail: bool = False,
) -> None:
    """Raise SystemExit if any validation is ``fail`` and ``allow_fail`` is False.

    Called by monitor scripts right after ``validate_sleeve`` runs. The banner is
    always printed first via caller — this function's job is the fail-closed gate.
    ``allow_fail=True`` provides an opt-out for callers who want to run through
    failures anyway (e.g., a diagnostic that specifically exercises how the monitor
    handles a broken symbol).
    """
    hard = [r for r in results if r.status == "fail"]
    if hard and not allow_fail:
        symbols = ", ".join(r.symbol for r in hard)
        raise SystemExit(
            f"[symbol-validate] REFUSING to start: {len(hard)} symbol(s) failed "
            f"pre-flight candle fetch: {symbols}. Remove them from the runbook or "
            f"pass allow_fail=True to skip this gate."
        )


__all__ = [
    "SymbolValidation",
    "ValidationStatus",
    "validate_sleeve",
    "format_validation_banner",
    "refuse_launch_on_hard_failures",
]
