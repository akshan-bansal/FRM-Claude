"""Combine the backtestable AI strategy-risk scalar with the live OSINT overlay scalar.

Two de-risk layers, composed the only honest way. The :class:`StrategyRiskModel` scalar is learned
from the strategy's own return history and is fully backtestable. The WorldMonitor OSINT scalar is a
*live* read with no point-in-time history, so it can never enter the historical model as a feature —
that would be lookahead. Instead the two meet at the **live decision edge**: multiply them (both lie
in ``[floor, 1]`` and only ever cut exposure) into one mitigation scalar, and halt if either the AI
model floors out or the OSINT overlay halts the asset class.

As the overlay journal (``state/intel_overlay.jsonl``) accumulates, OSINT indices gain a real
point-in-time history and *could* later join the model's feature set — until then they stay a live
multiplicative overlay on top of the backtestable core.
"""
from __future__ import annotations

from dataclasses import dataclass

from trading_live_claude.intel.overlay import OverlayDecision


@dataclass(frozen=True)
class MitigationDecision:
    scalar: float              # combined de-risk multiplier in [floor, 1]
    halt: bool                 # stand down new entries entirely
    strategy_scalar: float     # the AI strategy-risk component
    osint_scalar: float        # the live OSINT overlay component
    reasons: list[str]


def combine(strategy_scalar: float, osint: OverlayDecision | None, *, floor: float = 0.2,
            halt_below: float = 0.20) -> MitigationDecision:
    """Multiply the AI strategy-risk scalar by the live OSINT class scalar (de-risk only).

    ``osint`` may be ``None`` (overlay off or unavailable) — then only the AI scalar applies. The
    combined book is halted when the OSINT class is halted, or when the product falls at/under
    ``halt_below``.

    Note the strategy-risk gate cannot halt on its own by design: its floor (0.75) sits above
    ``halt_below`` (0.20), so the volatility rule can only ever trim size. Halting is reserved for the
    OSINT layer, which sees exogenous events the return stream cannot. Raising ``halt_below`` above
    the strategy floor would change that, and should be a deliberate decision rather than a side
    effect of retuning the floor.
    """
    s_ai = max(0.0, min(1.0, strategy_scalar))
    s_os = osint.scalar if osint is not None else 1.0
    combined = max(floor, s_ai * s_os)

    reasons: list[str] = []
    if s_ai < 0.9:
        reasons.append(f"AI strategy-risk model de-risking (x{s_ai:.2f})")
    if osint is not None and s_os < 0.9:
        cls = osint.asset_class
        drv = osint.reasons[0] if osint.reasons else "elevated risk"
        reasons.append(f"OSINT {cls} overlay (x{s_os:.2f}) — {drv}")

    halt = combined <= halt_below or (osint is not None and osint.halt_new_entries)
    return MitigationDecision(scalar=round(combined, 4), halt=halt,
                              strategy_scalar=round(s_ai, 4), osint_scalar=round(s_os, 4),
                              reasons=reasons or ["no elevated risk"])
