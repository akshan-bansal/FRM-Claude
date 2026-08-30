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
from typing import Literal

OverlayClass = Literal["equity", "future", "commodity", "fx", "crypto"]
OVERLAY_CLASSES: tuple[OverlayClass, ...] = ("equity", "future", "commodity", "fx", "crypto")


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
    halt_below: float = 0.4        # scalar at/below which new entries are stood down
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
    accel_lo: float = 1.5              # event-flow acceleration below which no de-risk
    accel_hi: float = 4.0              # ...and at/above which the acceleration gate bottoms out
    accel_floor: float = 0.75          # short-retention archive -> tilt only, max 25% trim
    fear_floor: float = 0.75           # gentlest gate: extreme fear trims at most 25%, since
    #                                    sentiment is a tilt, not a stop (own floor, not cfg.floor)


class RiskOverlay:
    """Turn a live :class:`IntelSnapshot` into per-asset-class :class:`OverlayDecision`s."""

    def __init__(self, config: OverlayConfig | None = None) -> None:
        self.cfg = config or OverlayConfig()

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
        else:  # crypto — high beta to global risk-off
            comps = {"global": g ** self.cfg.crypto_beta, "crypto_vol": self._crypto_vol_gate(s),
                     "fear": self._fear_gate(s), "economy": self._econ_gate(s),
                     "event_flow": self._accel_gate(s, "conflict", "military")}

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
