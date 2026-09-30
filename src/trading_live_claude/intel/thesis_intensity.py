"""Thesis intensity — how far, and for how long, a thesis's inputs have risen.

:func:`intel.interpret.interpret` answers a yes/no question per thesis: did a reading clear its
gate. This module grades the *yes*. Each input behind a thesis is scored on a bounded, positive,
concave log curve between a neutral floor and a stress ceiling (the **magnitude** dimension), and
the time the thesis has been firing continuously is scored on the same curve (the **time**
dimension). The log shape encodes diminishing sensitivity: the first move off the floor carries
the most information, and each further point of index or hour of persistence adds less.

    log_ramp(x; lo, hi) = ln(1 + k*u) / ln(1 + k),   u = clamp((x - lo) / (hi - lo), 0, 1)

With the default ``k = 9`` this is ``log10(1 + 9u)``: 0 at ``lo``, 1 at ``hi``, 0.28 at 10% of the
range, 0.74 at half of it. ``k`` sets the curvature (k -> 0 recovers a straight line).

    intensity = magnitude * (TIME_FLOOR + (1 - TIME_FLOOR) * time_score)

A thesis whose inputs just crossed their gates counts at ``TIME_FLOOR`` (half) weight; one that
has held for ``TIME_BOUND.hi`` hours counts at full weight. Legs combine as the thesis does: a
thesis that fires on *any* leg (OR) takes the strongest leg, one that needs *every* leg (AND)
takes the weakest.

**Informational only (2026-09-18).** Nothing here changes whether a thesis fires, its confidence
band, or the live-loop sizing trim. It is surfaced in thesis alerts and the heartbeat. Whether it
should drive any of those is an open decision.

**Bounds.** Measured on the 247 real snapshots in ``state/intel_overlay.jsonl``
(2026-08-29 -> 2026-09-18). Floors sit at the neutral level of each input; ceilings sit above the
observed maximum so the curve is not saturated by the one regime observed so far. See each
``Bound.basis``. The feed has no point-in-time history before 2026-08-29, so none of this is
backtested. It is a measurement convention, not a validated signal.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from trading_live_claude.curves import log_ramp
from trading_live_claude.intel.interpret import interpret
from trading_live_claude.intel.overlay import IntelSnapshot

TIME_FLOOR: float = 0.5         # weight of a reading that has only just started firing
MAX_GAP_HOURS: float = 3.0      # a longer hole in the snapshot history breaks a firing streak


@dataclass(frozen=True)
class Bound:
    lo: float
    hi: float
    basis: str


MAGNITUDE_BOUNDS: dict[str, Bound] = {
    "strategic_risk": Bound(60.0, 85.0,
                            "0-100 index. Floor 60 = original gate, below the observed min (64). "
                            "Ceiling 85 leaves headroom above the observed max (74)."),
    "conflict_events_active": Bound(2.0, 12.0,
                                    "Count. Floor 2 sits below p25 (3). Ceiling 12 is well above "
                                    "the observed max (7)."),
    "energy_accel": Bound(1.0, 6.0,
                          "Event flow / own baseline. 1.0 is baseline by definition. The observed "
                          "max is 6.33, so the ceiling saturates there."),
    "conflict_accel": Bound(1.0, 3.0,
                            "Flow / baseline. Observed max 1.20. The thesis leg sits at 2.5, the "
                            "ceiling just above it."),
    "energy_stress": Bound(0.3, 1.0,
                           "0-1 score. Floor 0.3 is the resting level (observed p90 0.29). "
                           "Ceiling is the scale max."),
    "natural_disasters_active": Bound(0.0, 10.0,
                                      "Count. Observed max 2. The high-confidence cutoff is 8, so the "
                                      "ceiling is 10."),
    "disaster_accel": Bound(1.0, 4.0,
                            "Flow / baseline. This key is absent from every observed payload so "
                            "far; the bound mirrors the thesis's 2.0 / 3.0 cutoffs."),
}

TIME_BOUND = Bound(0.0, 72.0, "Hours firing continuously. 72h = the graph journal's regime "
                              "horizon (wash cadence); a condition that has held 3 days is treated "
                              "as fully established.")

# Thesis name -> (how legs combine, input keys). Only theses driven by stress inputs that *rise*.
# Sentiment stretch is two-sided (greed or fear) and the dollar divergences read signed price
# changes present on ~1% of snapshots, so neither is modelled here.
THESIS_LEGS: dict[str, tuple[str, tuple[str, ...]]] = {
    "Complacency divergence": ("any", ("strategic_risk", "energy_accel", "conflict_accel")),
    "Energy event concentration": ("any", ("energy_accel", "energy_stress")),
    "Conflict escalation watch": ("any", ("conflict_events_active", "conflict_accel")),
    "Disaster / insurance underpricing": ("all", ("natural_disasters_active", "disaster_accel")),
    "Commodity carry-inversion proxy": ("all", ("energy_stress", "energy_accel")),
}


def leg_values(snap: IntelSnapshot) -> dict[str, float]:
    """The raw input behind each modelled leg, with the same defaults ``interpret`` uses."""
    accel = snap.event_acceleration or {}
    return {
        "strategic_risk": float(snap.strategic_risk),
        "conflict_events_active": float(snap.conflict_events_active),
        "energy_accel": float(accel.get("energy", 1.0)),
        "conflict_accel": float(accel.get("conflict", 1.0)),
        "energy_stress": float(snap.energy_stress),
        "natural_disasters_active": float(snap.natural_disasters_active),
        "disaster_accel": float(accel.get("disaster", 1.0)),
    }


def magnitude(thesis_name: str, snap: IntelSnapshot) -> tuple[float, dict[str, tuple[float, float]]]:
    """``(score, {leg: (raw value, leg score)})`` for a modelled thesis; ``(0.0, {})`` otherwise."""
    spec = THESIS_LEGS.get(thesis_name)
    if spec is None:
        return 0.0, {}
    mode, legs = spec
    raw = leg_values(snap)
    scored = {leg: (raw[leg], log_ramp(raw[leg], MAGNITUDE_BOUNDS[leg].lo, MAGNITUDE_BOUNDS[leg].hi))
              for leg in legs}
    scores = [s for _, s in scored.values()]
    return (max(scores) if mode == "any" else min(scores)), scored


def firing_hours(
    thesis_name: str,
    history: Sequence[tuple[datetime, IntelSnapshot]],
    now_fired: bool = True,
    max_gap_hours: float = MAX_GAP_HOURS,
) -> float:
    """Hours the thesis has fired continuously, ending at the newest history row.

    ``history`` is oldest-first. The streak walks back from the newest row while each row fired
    the thesis and no gap between consecutive rows exceeds ``max_gap_hours``. A missing hole in
    the record is treated as unknown, not as still-firing. Returns 0.0 when the newest row did
    not fire (or ``now_fired`` is False).
    """
    if not now_fired or not history:
        return 0.0
    newest_ts = history[-1][0]
    start_ts: datetime | None = None
    prev_ts: datetime | None = None
    for ts, snap in reversed(history):
        if prev_ts is not None and (prev_ts - ts).total_seconds() > max_gap_hours * 3600.0:
            break
        if thesis_name not in {t.name for t in interpret(snap)}:
            break
        start_ts = ts
        prev_ts = ts
    if start_ts is None:
        return 0.0
    return (newest_ts - start_ts).total_seconds() / 3600.0


@dataclass(frozen=True)
class ThesisIntensity:
    name: str
    magnitude: float
    hours: float
    time_score: float
    intensity: float
    legs: dict[str, tuple[float, float]] = field(default_factory=dict)

    def summary(self) -> str:
        """One line for alerts: the product and both factors, with the strongest leg named."""
        top = max(self.legs.items(), key=lambda kv: kv[1][1])[0] if self.legs else "-"
        return (f"intensity {self.intensity:.2f} = magnitude {self.magnitude:.2f} "
                f"(lead: {top}) x time {TIME_FLOOR + (1 - TIME_FLOOR) * self.time_score:.2f} "
                f"({self.hours:.1f}h firing)")


def intensity(
    thesis_name: str,
    snap: IntelSnapshot,
    history: Sequence[tuple[datetime, IntelSnapshot]] = (),
) -> ThesisIntensity | None:
    """Grade a firing thesis on the current snapshot plus its firing streak in ``history``.

    Returns None for theses this module does not model. ``history`` should end with ``snap``'s
    row (oldest-first); with no history the time factor sits at ``TIME_FLOOR``.
    """
    if thesis_name not in THESIS_LEGS:
        return None
    mag, legs = magnitude(thesis_name, snap)
    hours = firing_hours(thesis_name, history) if history else 0.0
    t_score = log_ramp(hours, TIME_BOUND.lo, TIME_BOUND.hi)
    value = mag * (TIME_FLOOR + (1.0 - TIME_FLOOR) * t_score)
    return ThesisIntensity(name=thesis_name, magnitude=mag, hours=hours, time_score=t_score,
                           intensity=value, legs=legs)


def _is_real(row: dict[str, Any]) -> bool:
    """Skip degraded rows and the empty rows that are not flagged degraded (no index, no VIX)."""
    s = row.get("snapshot") or {}
    if s.get("degraded") or row.get("degraded"):
        return False
    return bool(s.get("strategic_risk")) or (s.get("market") or {}).get("equity_vol") is not None


def load_history(
    path: str | Path = "state/intel_overlay.jsonl",
    *,
    since_hours: float = TIME_BOUND.hi + MAX_GAP_HOURS,
    now: datetime | None = None,
) -> list[tuple[datetime, IntelSnapshot]]:
    """Real snapshots from the overlay journal within ``since_hours`` of ``now``, oldest-first."""
    p = Path(path)
    if not p.exists():
        return []
    fields = {f for f in IntelSnapshot.__dataclass_fields__ if f != "as_of"}
    rows: list[tuple[datetime, IntelSnapshot]] = []
    for line in _lines(p):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not _is_real(row):
            continue
        ts = datetime.fromisoformat(row["as_of"])
        snap = IntelSnapshot(**{k: v for k, v in row["snapshot"].items() if k in fields})
        rows.append((ts, snap))
    rows.sort(key=lambda r: r[0])
    if rows:
        cutoff = (now or rows[-1][0]).timestamp() - since_hours * 3600.0
        rows = [r for r in rows if r[0].timestamp() >= cutoff]
    return rows


def _lines(p: Path) -> Iterable[str]:
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield line
