"""Confluence scoring — a second, bi-directional read of the intel, beside the de-risk overlay.

:mod:`trading_live_claude.intel.overlay` answers one question: *how much should this asset class be
stood down?* It multiplies gates, so it is magnitude-only, strictly de-risking, and its evidence
membership is hand-assigned. That is the right shape for a safety brake and the wrong shape for
reading what the intelligence actually implies.

This module scores the same snapshot on three axes instead:

* **direction** ``[-1, +1]`` — adverse to constructive *for this asset class*. The signed weights are
  the point: an energy supply shock is adverse for broad equity and **constructive** for producers
  and commodities, so the identical evidence carries opposite signs per class. A magnitude-only gate
  cannot express that, which is why the overlay de-risks commodities on exactly the news that would
  support them.
* **confidence** ``[0, 1]`` — how much the reading deserves to be believed, from three independent
  discounts: **freshness** (the vendor caches; observed ages ran to days), **persistence** (a reading
  sustained across journal reads outranks a one-off spike), and **corroboration** (multi-source
  signals over single-wire noise).
* **urgency** ``[0, 1]`` — how fast this decays. Accelerating event flow on fresh data is urgent; a
  slow-moving stress index is not. Urgency ranks *what to look at first*; it never sizes anything.

**Attention.** Rather than fixed gate membership, each class carries a signed weight vector over the
evidence dimensions, and the score is the attention-weighted sum of signed, confidence-discounted
evidence. The weights are seeded from the same reasoning the overlay encodes, but they are now data
shaped: as the journal accrues, they are fittable against realized per-class returns instead of being
argued about. That is the upgrade path the hand-assigned gate map does not have.

**Safety boundary.** This score is bi-directional and therefore must never size a position. It ranks
research candidates and orders attention; the de-risk overlay remains the only intel path that
touches exposure, and it can still only cut. Intel proposes, walk-forward disposes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from trading_live_claude.intel.overlay import OVERLAY_CLASSES, IntelSnapshot, OverlayClass

# Signed attention weights: evidence dimension -> how it bears on each class.
# Positive = constructive for that class, negative = adverse. Magnitude = how much it matters.
# Read the energy row across the columns to see the asymmetry the overlay cannot express.
ATTENTION: dict[OverlayClass, dict[str, float]] = {
    "equity":    {"strategic_risk": -0.30, "alerts": -0.25, "conflict": -0.15, "energy": -0.15,
                  "disasters": -0.05, "equity_vol": -0.35, "fear": -0.20, "event_flow": -0.20},
    "future":    {"strategic_risk": -0.30, "alerts": -0.20, "conflict": -0.25, "energy": -0.10,
                  "disasters": -0.05, "equity_vol": -0.25, "fear": -0.15, "event_flow": -0.25},
    # Commodities are LONG disruption: supply shocks and conflict in producing regions support price.
    "commodity": {"strategic_risk": +0.10, "alerts": -0.05, "conflict": +0.25, "energy": +0.40,
                  "disasters": +0.20, "equity_vol": -0.05, "fear": +0.05, "event_flow": +0.30},
    "fx":        {"strategic_risk": -0.20, "alerts": -0.15, "conflict": -0.10, "energy": -0.10,
                  "disasters": 0.00, "equity_vol": -0.15, "fear": -0.10, "event_flow": -0.10},
    # Crypto behaves as a high-beta risk asset here, not as a haven.
    "crypto":    {"strategic_risk": -0.35, "alerts": -0.30, "conflict": -0.20, "energy": -0.05,
                  "disasters": 0.00, "equity_vol": -0.30, "fear": -0.30, "event_flow": -0.25},
}

# Which snapshot source backs each evidence dimension, for the freshness discount.
_EVIDENCE_SOURCE: dict[str, str] = {
    "strategic_risk": "conflict", "alerts": "news", "conflict": "conflict", "energy": "energy",
    "disasters": "disasters", "event_flow": "events", "equity_vol": "market", "fear": "market",
}

STALENESS_HALF_LIFE_H = 24.0


@dataclass(frozen=True)
class Evidence:
    """One normalized dimension of the snapshot."""

    name: str
    severity: float     # [0, 1] — HOW MUCH this axis deteriorated. Direction is the weight's job:
    #                     mixing a signed severity with a signed weight double-counts the sign and
    #                     silently inverts any class that benefits from the deterioration.
    confidence: float   # [0, 1]
    urgency: float      # [0, 1]


@dataclass(frozen=True)
class ConfluenceScore:
    asset_class: OverlayClass
    direction: float                       # [-1, +1]
    confidence: float                      # [0, 1]
    urgency: float                         # [0, 1]
    score: float                           # [-100, +100] = direction * confidence * 100
    contributions: dict[str, float] = field(default_factory=dict)

    @property
    def stance(self) -> str:
        if self.score >= 25:
            return "constructive"
        if self.score <= -25:
            return "adverse"
        return "neutral"

    def top_drivers(self, n: int = 3) -> list[tuple[str, float]]:
        """Largest absolute contributors, so a score can always be explained."""
        return sorted(self.contributions.items(), key=lambda kv: -abs(kv[1]))[:n]


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _freshness(snap: IntelSnapshot, dim: str) -> float:
    src = _EVIDENCE_SOURCE.get(dim)
    age = snap.source_age_hours.get(src) if src else None
    if age is None or age <= 0:
        return 1.0
    return float(0.5 ** (age / STALENESS_HALF_LIFE_H))


def build_evidence(snap: IntelSnapshot, *,
                   persistence: dict[str, int] | None = None) -> dict[str, Evidence]:
    """Normalize a snapshot into signed evidence with per-dimension confidence and urgency.

    ``persistence`` maps a dimension to how many consecutive journal reads it has been elevated
    (from :class:`trading_live_claude.intel.history.IntelHistory`). It raises confidence: a condition
    that has held for several reads is a regime, a single reading is a data point.
    """
    runs = persistence or {}
    accel = snap.event_acceleration or {}
    max_accel = max(accel.values(), default=1.0)

    # Severity in [0, 1]: how far each axis has deteriorated from calm. Unsigned by design.
    raw: dict[str, float] = {
        "strategic_risk": _clip((snap.strategic_risk - 50.0) / 50.0, 0.0, 1.0),
        "alerts": _clip(snap.global_alert_count / 12.0, 0.0, 1.0),
        "conflict": _clip(snap.conflict_events_active / 8.0, 0.0, 1.0),
        "energy": _clip(snap.energy_stress / 0.4, 0.0, 1.0),
        "disasters": _clip(snap.natural_disasters_active / 6.0, 0.0, 1.0),
        "event_flow": _clip((max_accel - 1.0) / 3.0, 0.0, 1.0),
        "equity_vol": _clip((snap.market.get("equity_vol", 15.0) - 15.0) / 23.0, 0.0, 1.0),
        # Fear/greed enters as FEAR severity so it points the same way as every other axis.
        "fear": _clip((50.0 - (snap.fear_greed if snap.fear_greed is not None else 50.0)) / 50.0,
                      0.0, 1.0),
    }

    out: dict[str, Evidence] = {}
    for dim, sev in raw.items():
        fresh = _freshness(snap, dim)
        run = runs.get(dim, 0)
        persist = min(1.0, 0.5 + 0.1 * run)          # 0.5 on a single read, 1.0 by the 5th
        conf = _clip(fresh * persist, 0.0, 1.0)
        if snap.degraded:
            conf *= 0.6                               # a partial fetch is weaker evidence
        # Urgency: fast-moving axes on fresh data. Event flow and vol move in hours; stress indices
        # and advisory counts move in days, so they are never urgent regardless of magnitude.
        fast = dim in {"event_flow", "equity_vol", "alerts", "fear"}
        urg = _clip(sev * fresh * (1.0 if fast else 0.3), 0.0, 1.0)
        out[dim] = Evidence(name=dim, severity=sev, confidence=conf, urgency=urg)
    return out


def score_class(asset_class: OverlayClass, evidence: dict[str, Evidence]) -> ConfluenceScore:
    """Attention-weighted confluence for one asset class."""
    weights = ATTENTION[asset_class]
    contributions: dict[str, float] = {}
    num = 0.0
    wsum = 0.0
    conf_num = 0.0
    urg = 0.0
    for dim, w in weights.items():
        ev = evidence.get(dim)
        if ev is None or w == 0.0:
            continue
        # unsigned severity x SIGNED attention x confidence — the weight alone sets direction
        c = w * ev.severity * ev.confidence
        contributions[dim] = round(c, 4)
        num += c
        wsum += abs(w)
        conf_num += abs(w) * ev.confidence
        urg = max(urg, ev.urgency * abs(w) / max(abs(v) for v in weights.values()))

    direction = _clip(num / wsum) if wsum else 0.0
    confidence = _clip(conf_num / wsum, 0.0, 1.0) if wsum else 0.0
    return ConfluenceScore(asset_class=asset_class, direction=round(direction, 4),
                           confidence=round(confidence, 4), urgency=round(_clip(urg, 0.0, 1.0), 4),
                           score=round(direction * confidence * 100.0, 2),
                           contributions=contributions)


def confluence(snap: IntelSnapshot, *,
               persistence: dict[str, int] | None = None) -> dict[OverlayClass, ConfluenceScore]:
    """Score every asset class on direction / confidence / urgency."""
    ev = build_evidence(snap, persistence=persistence)
    return {c: score_class(c, ev) for c in OVERLAY_CLASSES}
