"""WorldMonitor risk overlay — live OSINT intelligence → per-asset-class de-risk decisions.

WorldMonitor has no point-in-time history, so it can never be a backtestable alpha signal; feeding
it into a backtest would be lookahead. It is used the honest way instead: as a **live risk overlay**
that can only *reduce* exposure. It never generates an entry and never adds leverage — an untested
exogenous input is only ever allowed to stand the book down.

The mechanism mirrors :class:`trading_live_claude.models.regime.RegimeClassifier`: several gates,
each a ramp into ``[floor, 1]``, multiplied into one **risk scalar** per asset class. The scalar
multiplies gross exposure exactly where the market-regime scalar already does. Each asset class reads
the intelligence relevant to it:

* **equity**  — global news alerts, economic-alert density, equity-vol proxy (VIX).
* **future**  — global alerts, energy stress and active conflict (index/commodity futures blend).
* **commodity** — energy stress (heaviest), conflict in producing regions, major natural disasters.
* **fx**      — economic-alert density, the dollar-index move, mild conflict sensitivity.
* **crypto**  — global risk-off with a high-beta exponent, crypto-vol proxy, economic alerts.

Scoring is a **pure function** of the normalized :class:`IntelSnapshot`, so it is fully unit-tested
without any network. When a fetch was incomplete the snapshot is ``degraded`` and every class is
capped conservatively — the overlay fails safe (toward less risk), never optimistic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import ClassVar, Literal

OverlayClass = Literal[
    "equity", "future", "commodity", "fx", "crypto",
    "fixed_income", "precious_metals",
]
OVERLAY_CLASSES: tuple[OverlayClass, ...] = (
    "equity", "future", "commodity", "fx", "crypto",
    "fixed_income", "precious_metals",
)


@dataclass(frozen=True)
class IntelSnapshot:
    """Normalized, numeric features extracted from the live WorldMonitor artifacts.

    Only structured fields — counts, scores, category labels — never free text. Built by
    :meth:`trading_live_claude.intel.worldmonitor.WorldMonitorClient.snapshot`, but a plain instance
    is all the overlay needs, which is what makes the scoring testable offline.
    """
    global_alert_count: int = 0
    global_max_importance: float = 0.0
    category_alert_counts: dict[str, int] = field(default_factory=dict)
    country_alert_counts: dict[str, int] = field(default_factory=dict)
    conflict_events_active: int = 0            # calibrated escalations (critical cross-source signals)
    natural_disasters_active: int = 0
    energy_stress: float = 0.0                 # [0, 1]
    strategic_risk: float = 0.0                # WorldMonitor global geopolitical index, 0-100
    event_acceleration: dict[str, float] = field(default_factory=dict)  # domain -> recent/baseline event flow
    source_age_hours: dict[str, float] = field(default_factory=dict)    # tool -> age of its cached payload
    fear_greed: float | None = None            # market fear/greed composite, 0-100 (low = fear/risk-off)
    market: dict[str, float] = field(default_factory=dict)  # equity_vol, dxy, crypto, *_chg
    degraded: bool = False
    as_of: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class OverlayDecision:
    asset_class: OverlayClass
    scalar: float                # [floor, 1] — multiply this class's gross by it
    halt_new_entries: bool       # stand down new risk in this class
    reasons: list[str]           # human-readable drivers of any reduction
    components: dict[str, float]  # per-gate scalar, for charting / audit


def _ramp(x: float, lo: float, hi: float, y_lo: float, y_hi: float) -> float:
    """Linear ramp of x from (lo->y_lo) to (hi->y_hi), clamped outside [lo, hi]."""
    if hi == lo:
        return y_hi
    t = (x - lo) / (hi - lo)
    t = min(1.0, max(0.0, t))
    return y_lo + t * (y_hi - y_lo)


@dataclass(frozen=True)
class OverlayConfig:
    floor: float = 0.25            # smallest per-gate / per-class scalar (never fully zero)
    # Halt threshold. NOTE the interaction with ``floor``: a class scalar is clamped to >= floor,
    # so a halt_below at or under the floor is unreachable and halting never fires. At the current
    # floor (0.25) this 0.20 setting means the overlay trims but no longer stands a class down;
    # drop the floor below 0.20 to make it a live threshold again.
    halt_below: float = 0.20       # scalar at/below which new entries are stood down
    degraded_cap: float = 0.8      # when the snapshot is degraded, cap every class here
    # gate saturation points (where a gate bottoms out at floor)
    alerts_full: float = 12.0      # global alert count → full de-risk
    conflict_full: float = 8.0
    disaster_full: float = 6.0
    econ_alerts_full: float = 6.0
    vix_lo: float = 15.0
    vix_hi: float = 38.0
    crypto_vol_chg_full: float = 8.0   # |BTC daily %| at which crypto-vol gate bottoms
    dxy_chg_full: float = 1.5          # |DXY daily %| at which the fx gate bottoms
    crypto_beta: float = 1.6           # exponent on the global gate for crypto (high beta)
    strat_lo: float = 60.0             # global geopolitical index below which no de-risk (MEDIUM~65)
    strat_hi: float = 90.0             # ...and at/above which the geo gate bottoms out (SEVERE)
    fear_hi: float = 45.0              # fear/greed at/above which no de-risk (>=45 = neutral/greed)
    fear_lo: float = 18.0              # ...and at/below which the fear gate bottoms out (extreme fear)
    # Freshness decay. The vendor serves CACHED payloads and stamps each with `cached_at`; observed
    # ages ranged from minutes (news) to ~4 days (energy). A gate driven by a four-day-old reading
    # should not carry the same authority as one driven by a live one, so each gate's DEVIATION from
    # neutral decays with the age of the source behind it. Half-life, in hours.
    staleness_half_life_h: float = 24.0
    accel_lo: float = 1.5              # event-flow acceleration below which no de-risk
    accel_hi: float = 4.0              # ...and at/above which the acceleration gate bottoms out
    accel_floor: float = 0.75          # short-retention archive -> tilt only, max 25% trim
    fear_floor: float = 0.75           # gentlest gate: extreme fear trims at most 25%, since
    #                                    sentiment is a tilt, not a stop (own floor, not cfg.floor)


class RiskOverlay:
    """Turn a live :class:`IntelSnapshot` into per-asset-class :class:`OverlayDecision`s."""

    def __init__(self, config: OverlayConfig | None = None) -> None:
        self.cfg = config or OverlayConfig()

    # ---- freshness ----------------------------------------------------------
    # Which payload sources drive each gate, with a base weight per source. Two purposes: (a) the
    # market-derived gates (fear, VIX, DXY, crypto_vol) now carry a ``market`` source explicitly
    # rather than silently returning 1.0 as they used to; (b) gates that will eventually blend
    # multiple payloads (e.g. supply stress reading both news and energy) can list them here and
    # be weighted honestly. Single-source entries preserve the previous behavior exactly.
    _GATE_SOURCES: ClassVar[dict[str, tuple[tuple[str, float], ...]]] = {
        "global": (("news", 1.0),),
        "economy": (("news", 1.0),),
        "conflict": (("conflict", 1.0),),
        "disaster": (("disasters", 1.0),),
        "energy": (("energy", 1.0),),
        "event_flow": (("events", 1.0),),
        # Market-driven gates — previously uncovered, now discounted by the market payload age.
        "fear": (("market", 1.0),),
        "equity_vol": (("market", 1.0),),
        "dxy": (("market", 1.0),),
        "crypto_vol": (("market", 1.0),),
    }

    def _freshness(self, s: IntelSnapshot, gate: str) -> float:
        """Freshness weight in [0, 1] for a gate, inverse-weighted by staleness across sources.

        Each source contributes its own freshness ``f_i = 0.5 ** (age_i / half_life)`` but weighted
        by ``base_weight * f_i`` — the SAME freshness that determines its contribution. The blended
        freshness is therefore ``sum(w_i * f_i^2) / sum(w_i * f_i)``. A stale source's small weight
        means it drags the average down less than a naive mean would: with one fresh source
        (f=1.0) and one four-half-lives-stale source (f=0.06), the blended weight is ~0.94, so the
        gate stays trustworthy on the fresh side rather than being pulled toward the median.

        Single-source entries reduce to the earlier ``0.5 ** (age/half_life)`` exactly. Unknown or
        zero age is treated as fresh (the common case for a live-computed field with no stamp).
        """
        sources = self._GATE_SOURCES.get(gate)
        if not sources:
            return 1.0
        hl = self.cfg.staleness_half_life_h
        num = 0.0
        den = 0.0
        for src, base_w in sources:
            age = s.source_age_hours.get(src)
            f = 1.0 if age is None or age <= 0 else float(0.5 ** (age / hl))
            effective_w = base_w * f
            num += effective_w * f
            den += effective_w
        if den <= 0:
            return 1.0
        return num / den

    def _decay(self, gate_value: float, weight: float) -> float:
        """Pull a gate toward neutral (1.0) in proportion to how stale its source is."""
        return 1.0 - weight * (1.0 - gate_value)

    # ---- individual gates (each returns a scalar in [floor, 1]) --------------
    def _global_gate(self, s: IntelSnapshot) -> float:
        c = self.cfg
        by_count = _ramp(s.global_alert_count, 0.0, c.alerts_full, 1.0, c.floor)
        # a single very-high-importance alert also bites
        by_imp = _ramp(s.global_max_importance, 0.6, 1.0, 1.0, 0.6) if s.global_max_importance else 1.0
        # WorldMonitor's calibrated geopolitical index (0-100). Default 0 -> ramp clamps to 1 (no effect).
        by_strat = _ramp(s.strategic_risk, c.strat_lo, c.strat_hi, 1.0, c.floor) if s.strategic_risk else 1.0
        return min(by_count, by_imp, by_strat)

    def _accel_gate(self, s: IntelSnapshot, *domains: str) -> float:
        """De-risk as event flow in the given domains accelerates past its own recent baseline.

        Deliberately gentle (``accel_floor``): the archive behind this is short-retention, so it is
        treated as a situational tilt, never a validated signal.
        """
        vals = [s.event_acceleration.get(d, 1.0) for d in domains if d in s.event_acceleration]
        if not vals:
            return 1.0
        worst = max(vals)
        return _ramp(worst, self.cfg.accel_lo, self.cfg.accel_hi, 1.0, self.cfg.accel_floor)

    def _fear_gate(self, s: IntelSnapshot) -> float:
        """Market fear/greed (0-100): low = fear/risk-off -> de-risk. None or >= fear_hi -> no effect."""
        if s.fear_greed is None:
            return 1.0
        return _ramp(s.fear_greed, self.cfg.fear_hi, self.cfg.fear_lo, 1.0, self.cfg.fear_floor)

    def _conflict_gate(self, s: IntelSnapshot) -> float:
        return _ramp(s.conflict_events_active, 0.0, self.cfg.conflict_full, 1.0, self.cfg.floor)

    def _disaster_gate(self, s: IntelSnapshot) -> float:
        return _ramp(s.natural_disasters_active, 0.0, self.cfg.disaster_full, 1.0, self.cfg.floor)

    def _energy_gate(self, s: IntelSnapshot) -> float:
        return _ramp(s.energy_stress, 0.0, 1.0, 1.0, self.cfg.floor)

    def _econ_gate(self, s: IntelSnapshot) -> float:
        n = s.category_alert_counts.get("economy", 0) + s.category_alert_counts.get("economic", 0)
        return _ramp(n, 0.0, self.cfg.econ_alerts_full, 1.0, self.cfg.floor)

    def _vix_gate(self, s: IntelSnapshot) -> float:
        vix = s.market.get("equity_vol", 0.0)
        return _ramp(vix, self.cfg.vix_lo, self.cfg.vix_hi, 1.0, self.cfg.floor) if vix else 1.0

    def _crypto_vol_gate(self, s: IntelSnapshot) -> float:
        chg = abs(s.market.get("crypto_chg", 0.0))
        return _ramp(chg, 0.0, self.cfg.crypto_vol_chg_full, 1.0, self.cfg.floor) if chg else 1.0

    def _dxy_gate(self, s: IntelSnapshot) -> float:
        chg = abs(s.market.get("dxy_chg", 0.0))
        return _ramp(chg, 0.0, self.cfg.dxy_chg_full, 1.0, self.cfg.floor) if chg else 1.0

    # ---- per-class composition ----------------------------------------------
    def _compose(self, asset_class: OverlayClass, s: IntelSnapshot) -> OverlayDecision:
        g = self._global_gate(s)
        comps: dict[str, float]
        if asset_class == "equity":
            comps = {"global": g, "economy": self._econ_gate(s), "equity_vol": self._vix_gate(s),
                     "fear": self._fear_gate(s), "conflict": _blend(self._conflict_gate(s), 0.5),
                     "event_flow": self._accel_gate(s, "conflict", "military")}
        elif asset_class == "future":
            comps = {"global": g, "energy": self._energy_gate(s),
                     "conflict": self._conflict_gate(s), "equity_vol": _blend(self._vix_gate(s), 0.5),
                     "event_flow": self._accel_gate(s, "conflict", "military", "energy")}
        elif asset_class == "commodity":
            comps = {"global": _blend(g, 0.5), "energy": self._energy_gate(s),
                     "conflict": self._conflict_gate(s), "disaster": self._disaster_gate(s),
                     "event_flow": self._accel_gate(s, "energy", "conflict")}
        elif asset_class == "fx":
            comps = {"global": g, "economy": self._econ_gate(s), "dxy": self._dxy_gate(s),
                     "conflict": _blend(self._conflict_gate(s), 0.5),
                     "event_flow": self._accel_gate(s, "conflict", "energy")}
        elif asset_class == "fixed_income":
            # Bonds have their own risk story. Rate/economy stress lifts them (flight-to-quality),
            # so the economy and global gates apply only lightly — this is a de-risking framework,
            # never a lean-in one, but we don't want to trim bonds as hard as we trim equity when
            # the overlay is telling us the same news that usually rallies duration. Conflict is a
            # safe-haven trigger for treasuries, so its weight is halved.
            comps = {"global": _blend(g, 0.4), "economy": _blend(self._econ_gate(s), 0.5),
                     "conflict": _blend(self._conflict_gate(s), 0.3),
                     "event_flow": _blend(self._accel_gate(s, "conflict", "military"), 0.5)}
        elif asset_class == "precious_metals":
            # Gold / silver / platinum are safe-haven assets. Their strongest exposure is to a
            # sharp dollar move — the dxy gate applies at full weight. Conflict and disaster are
            # buying triggers, so their gates apply lightly; global risk-off gets partial weight
            # because a systemic squeeze (2008-style) can still take metals down alongside equity.
            comps = {"global": _blend(g, 0.3), "dxy": self._dxy_gate(s),
                     "conflict": _blend(self._conflict_gate(s), 0.3),
                     "disaster": _blend(self._disaster_gate(s), 0.3),
                     "event_flow": _blend(self._accel_gate(s, "conflict", "energy"), 0.5)}
        else:  # crypto — high beta to global risk-off
            comps = {"global": g ** self.cfg.crypto_beta, "crypto_vol": self._crypto_vol_gate(s),
                     "fear": self._fear_gate(s), "economy": self._econ_gate(s),
                     "event_flow": self._accel_gate(s, "conflict", "military")}

        # Discount each gate by the freshness of the payload driving it, BEFORE multiplying. A stale
        # source therefore weakens its own gate rather than the whole class scalar.
        comps = {k: self._decay(v, self._freshness(s, k)) for k, v in comps.items()}

        scalar = 1.0
        for v in comps.values():
            scalar *= v
        scalar = max(self.cfg.floor, min(1.0, scalar))

        reasons: list[str] = []
        if s.degraded:
            scalar = min(scalar, self.cfg.degraded_cap)
            reasons.append("intelligence feed degraded — conservative cap applied")
        reasons.extend(_gate_reasons(comps, s))

        return OverlayDecision(asset_class=asset_class, scalar=round(scalar, 4),
                               halt_new_entries=scalar <= self.cfg.halt_below,
                               reasons=reasons, components={k: round(v, 4) for k, v in comps.items()})

    def evaluate(self, snapshot: IntelSnapshot) -> dict[OverlayClass, OverlayDecision]:
        """Per-asset-class overlay decisions for the current live snapshot."""
        return {c: self._compose(c, snapshot) for c in OVERLAY_CLASSES}


def _blend(gate: float, weight: float) -> float:
    """Dilute a gate toward 1 (no effect) by ``weight`` — a class only partly sensitive to it."""
    return 1.0 - weight * (1.0 - gate)


_GATE_LABEL = {
    "global": "elevated global news alerts",
    "economy": "economic-stress alerts",
    "equity_vol": "elevated equity volatility (VIX)",
    "conflict": "active armed-conflict events",
    "energy": "energy-supply stress",
    "disaster": "major natural disasters",
    "dxy": "sharp US-dollar move",
    "crypto_vol": "elevated crypto volatility",
    "fear": "market fear (low fear/greed)",
    "event_flow": "accelerating OSINT event flow",
}


def _gate_reasons(comps: dict[str, float], s: IntelSnapshot) -> list[str]:
    out: list[str] = []
    for name, v in sorted(comps.items(), key=lambda kv: kv[1]):
        if v < 0.9:  # only material reducers
            out.append(f"{_GATE_LABEL.get(name, name)} (x{v:.2f})")
    return out
