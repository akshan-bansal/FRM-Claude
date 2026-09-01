"""Interpret an OSINT snapshot into named theses — what the intelligence actually implies.

The overlay in :mod:`trading_live_claude.intel.overlay` turns a snapshot into *numbers* (per-class
de-risk scalars). This module turns the same snapshot into *reasoning*: it looks for configurations
across independent inputs that mean something more than any single gate does, and states each as a
thesis with its evidence, the inference drawn, and the exposures implicated.

The most valuable configurations are **divergences**, where two inputs disagree. A high geopolitical
risk index alongside a greedy sentiment composite and a low VIX is not two separate readings — it is
one observation: exogenous risk is building while the market is pricing calm. That inference exists
in neither gate alone, which is exactly why the multiplicative overlay cannot express it.

**These are hypotheses, not signals.** Nothing here is backtested — the OSINT feed has no usable
point-in-time history (see :mod:`trading_live_claude.intel.events`), so a thesis is a direction to
investigate, and anything traded on it must still clear the walk-forward like any other candidate.
Each thesis therefore carries an explicit ``action`` framed as risk posture or research focus, never
as an entry signal.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from trading_live_claude.intel.overlay import IntelSnapshot

# Themes an intel domain implicates, as ticker exemplars already present in this project's universe.
# Deliberately small and explicit: these are starting points for research, not a sector database.
THEME_EXEMPLARS: dict[str, tuple[str, ...]] = {
    "energy": ("XLE", "USO", "UNG", "CNQ.TO", "ENB.TO", "GEI.TO", "TA.TO", "FRU.TO", "ARX.TO"),
    "defense_geopolitical": ("ITA", "LMT", "RTX", "NOC", "GD"),
    "safe_haven": ("GLD", "CGL.TO", "SLV", "PAXG/USD", "XLU"),
    "volatility_convexity": ("VIXY", "UVXY", "SPY puts"),
    "materials": ("XLB", "FCX", "ABX.TO", "AEM.TO", "TECK.B.TO"),
    "dollar": ("UUP", "USDCAD", "DX-Y.NYB"),
    # NEW keys for the added theses. ``insurance`` covers global-catastrophe-exposed lines rather
    # than everyone with an insurance ticker; ``emerging_markets`` seeds a research direction, not
    # a currency-specific bet.
    "insurance": ("RE", "AIG", "ALL", "CB", "TRV", "IFC.TO", "MFC.TO"),
    "emerging_markets": ("EEM", "EWZ", "VWO", "IEMG", "XEM.TO"),
}


@dataclass(frozen=True)
class Thesis:
    """One named interpretation of the intelligence picture."""

    name: str
    confidence: str            # "high" | "moderate" | "tentative" — evidential strength, not odds
    evidence: list[str]        # the concrete readings that triggered it
    inference: str             # what those readings jointly imply
    action: str                # risk posture / research focus — never an entry signal
    themes: list[str] = field(default_factory=list)   # keys into THEME_EXEMPLARS

    def exemplars(self) -> list[str]:
        out: list[str] = []
        for t in self.themes:
            out.extend(THEME_EXEMPLARS.get(t, ()))
        return out


def _fmt(v: float | None, suffix: str = "") -> str:
    return "n/a" if v is None else f"{v:.1f}{suffix}"


def interpret(snap: IntelSnapshot) -> list[Thesis]:
    """Derive theses from an OSINT snapshot, strongest evidence first.

    Returns an empty-ish list (just the quiet-tape read) when nothing notable configures — a clean
    picture is itself information, and inventing a thesis from noise is the failure mode here.
    """
    out: list[Thesis] = []
    vix = snap.market.get("equity_vol")
    fg = snap.fear_greed
    accel = snap.event_acceleration or {}
    energy_accel = accel.get("energy", 1.0)
    conflict_accel = accel.get("conflict", 1.0)
    military_accel = accel.get("military", 1.0)

    # --- 1. Complacency divergence: exogenous risk building while the market prices calm ---------
    calm_market = (vix is not None and vix < 18.0) or (fg is not None and fg >= 60.0)
    stressed_world = snap.strategic_risk >= 60.0 or energy_accel >= 2.0 or conflict_accel >= 2.0
    if calm_market and stressed_world:
        ev = []
        if snap.strategic_risk:
            ev.append(f"geopolitical strategic-risk index {snap.strategic_risk:.0f}/100")
        if energy_accel >= 2.0:
            ev.append(f"energy event flow {energy_accel:.1f}x its own baseline")
        if conflict_accel >= 2.0:
            ev.append(f"conflict event flow {conflict_accel:.1f}x baseline")
        if vix is not None:
            ev.append(f"VIX {vix:.1f} (low)")
        if fg is not None:
            ev.append(f"fear/greed {fg:.0f} (greed)")
        out.append(Thesis(
            name="Complacency divergence",
            confidence="high" if (snap.strategic_risk >= 65 and energy_accel >= 3.0) else "moderate",
            evidence=ev,
            inference="Exogenous risk is building while the market prices calm. The two readings are "
                      "independent — event flow comes from the OSINT archive, VIX and sentiment from "
                      "market data — so this is a genuine disagreement, not one signal counted twice. "
                      "It means protection is cheap precisely while the driver of risk is rising, and "
                      "that a repricing has not happened yet rather than that it will not.",
            action="Favour cheap convexity over outright de-risking: hedges and optionality cost "
                   "little at this VIX. Treat new risk-taking in the implicated themes as needing a "
                   "wider margin of safety.",
            themes=["volatility_convexity", "safe_haven"],
        ))

    # --- 1b. Dollar strength divergence --------------------------------------------------------
    # A stronger USD usually pressures USD-denominated commodities and EM assets. When both are
    # RISING together anyway, one of them is wrong — that's the divergence, and it usually resolves
    # against the risk asset. Fires on the primary case (strong dollar + risk-on) and the mirror
    # case (weak dollar + no commodity strength → the sell-off is about demand, not currency).
    dxy_chg = snap.market.get("dxy_chg")
    crypto_chg = snap.market.get("crypto_chg")
    if dxy_chg is not None:
        strong_dollar = dxy_chg >= 0.4        # % change; positive and material
        weak_dollar = dxy_chg <= -0.4
        risk_on = (crypto_chg is not None and crypto_chg >= 1.0) or energy_accel >= 2.0
        commodity_weakness = snap.energy_stress < 0.2 and energy_accel < 1.2
        if strong_dollar and risk_on:
            out.append(Thesis(
                name="Dollar strength divergence — risk-on into a strong USD",
                confidence="tentative",
                evidence=[f"DXY change {dxy_chg:+.2f}%",
                          f"crypto change {crypto_chg:+.2f}%" if crypto_chg is not None else
                          f"energy event flow {energy_accel:.1f}x baseline"],
                inference="A strengthening dollar usually pressures USD-denominated commodities and "
                          "EM assets down; when they are rallying together, one side has to give. "
                          "Historically the risk asset gives first, so the tape's optimism is on "
                          "borrowed time rather than confirmed by the currency.",
                action="Prefer trimming into commodity / EM strength here. If exposure is being "
                       "added, size against the FX gate and check for the divergence resolving "
                       "before adding more.",
                themes=["dollar", "materials", "emerging_markets"],
            ))
        elif weak_dollar and commodity_weakness:
            out.append(Thesis(
                name="Dollar weakness divergence — demand rather than currency",
                confidence="tentative",
                evidence=[f"DXY change {dxy_chg:+.2f}%",
                          f"energy stress {snap.energy_stress:.2f}",
                          f"energy event flow {energy_accel:.1f}x baseline"],
                inference="A weakening dollar normally lifts USD-denominated commodities; when they "
                          "are flat or falling anyway, the driver is demand destruction rather than "
                          "currency. That is a very different regime from an FX-led rally.",
                action="Research focus on demand-sensitive names (consumer discretionary, industrial "
                       "commodities) rather than the pure FX beneficiaries. Do not size on the "
                       "assumption that a lower dollar automatically supports commodities here.",
                themes=["dollar", "materials"],
            ))

    # --- 2. Energy shock: the domain where flow is actually concentrated -------------------------
    if energy_accel >= 2.0 or snap.energy_stress >= 0.3:
        out.append(Thesis(
            name="Energy event concentration",
            confidence="high" if energy_accel >= 4.0 else "moderate",
            evidence=[f"energy event flow {energy_accel:.1f}x baseline",
                      f"energy-supply stress {snap.energy_stress:.2f}",
                      f"conflict flow {conflict_accel:.1f}x, military flow {military_accel:.1f}x"],
            inference="Event flow is concentrated in energy rather than spread across domains, and it "
                      "is running well ahead of conflict and military flow. A single-domain surge "
                      "usually reflects supply/infrastructure news rather than broad geopolitical "
                      "escalation, which points at energy-specific pricing rather than a risk-off tape. "
                      "Note this de-risks commodities in the overlay for a reason unrelated to price "
                      "volatility — realized commodity vol has not moved.",
            action="Research focus on energy names, in both directions: supply disruption supports "
                   "producers while demand destruction hurts consumers-of-energy. Size any energy "
                   "exposure against the overlay's reduced commodity scalar.",
            themes=["energy", "materials"],
        ))

    # --- 3. Conflict escalation --------------------------------------------------------------
    if snap.conflict_events_active >= 3 or conflict_accel >= 2.5:
        out.append(Thesis(
            name="Conflict escalation watch",
            confidence="moderate" if snap.conflict_events_active >= 5 else "tentative",
            evidence=[f"{snap.conflict_events_active} critical cross-source escalations",
                      f"conflict event flow {conflict_accel:.1f}x baseline",
                      f"{len(snap.country_alert_counts)} countries carrying advisories"],
            inference="Multiple independent sources are corroborating escalation. Corroboration across "
                      "sources matters more than raw counts here, since a single wire story can "
                      "inflate any one feed.",
            action="Watch defense and safe-haven exposure as a hedge on escalation, not as a directional "
                   "bet. Re-check before adding risk in the affected geographies.",
            themes=["defense_geopolitical", "safe_haven"],
        ))

    # --- 3b. Disaster / insurance underpricing -------------------------------------------------
    # The same complacency shape as thesis 1 but on a different independent pair: physical disaster
    # activity vs. market implied vol. When disasters are accelerating while VIX has not moved,
    # catastrophe-exposed lines are the cleanest concentrated bet on the repricing that has yet to
    # happen. Deliberately narrow theme mapping — insurance + materials for reconstruction, not the
    # defense-geopolitical hedge which is unrelated.
    disaster_accel = accel.get("disaster", 1.0)
    if (snap.natural_disasters_active >= 5 and disaster_accel >= 2.0
            and vix is not None and vix < 20.0):
        out.append(Thesis(
            name="Disaster / insurance underpricing",
            confidence="high" if snap.natural_disasters_active >= 8 and disaster_accel >= 3.0
                      else "moderate",
            evidence=[f"{snap.natural_disasters_active} active natural disasters",
                      f"disaster event flow {disaster_accel:.1f}x baseline",
                      f"VIX {vix:.1f} (calm)"],
            inference="Physical-world disaster activity is elevated and accelerating while the "
                      "market's aggregate implied vol has not moved. Catastrophe-exposed lines "
                      "(reinsurers, some primary carriers, catastrophe-exposed utilities) price on "
                      "a specific tail that is materializing, and the market gauge does not see it.",
            action="Research focus on reinsurance and cat-exposed insurers as a specific bet on the "
                   "repricing catching up. Reconstruction materials are the second-order angle. "
                   "The divergence is narrow — it does not support a broad-market bearish posture.",
            themes=["insurance", "materials"],
        ))

    # --- 4. Sentiment stretch (contrarian read on the crowd) -----------------------------------
    if fg is not None and (fg >= 70.0 or fg <= 25.0):
        greedy = fg >= 70.0
        out.append(Thesis(
            name="Sentiment stretch — " + ("greed" if greedy else "fear"),
            confidence="tentative",
            evidence=[f"fear/greed composite {fg:.0f}"],
            inference=("Crowd positioning is stretched toward greed, which historically thins the "
                       "buffer against bad news rather than predicting its arrival."
                       if greedy else
                       "Crowd positioning is stretched toward fear, which is where forward returns "
                       "have historically been better, not worse."),
            action=("Prefer trimming into strength over chasing it; the overlay's fear gate does not "
                    "de-risk on greed, so this is a discretionary caution only."
                    if greedy else
                    "Extreme fear is when the de-risk gates bind hardest — check that the overlay is "
                    "not standing you down at precisely the wrong moment."),
            themes=["volatility_convexity"] if greedy else [],
        ))

    # --- 4b. Commodity carry-inversion proxy ---------------------------------------------------
    # The clean signal here is a shift in the near-vs-deferred futures curve — a real proxy for
    # supply stress moving from headline to physical pricing. We do NOT have a live futures-curve
    # feed in this project yet, so this fires on the strongest available proxy: sustained high
    # energy-supply stress AND accelerating energy events together, which is where a curve would
    # typically be inverting. Explicitly framed as a proxy — the action text says so, and the
    # confidence tops out at "moderate". Real futures data is queued as a follow-up.
    if snap.energy_stress >= 0.5 and energy_accel >= 3.0:
        out.append(Thesis(
            name="Commodity carry-inversion proxy",
            confidence="moderate",       # capped: this is a proxy, not the real signal
            evidence=[f"energy-supply stress {snap.energy_stress:.2f}",
                      f"energy event flow {energy_accel:.1f}x baseline",
                      "curve data not yet ingested — proxy fires on stress + acceleration"],
            inference="A futures curve inverting from contango to backwardation is the cleanest "
                      "signal that supply stress has moved from headline into physical pricing. "
                      "This project has no live curve feed yet, so the combination of sustained "
                      "high supply stress and accelerating flow is used as a proxy for the same "
                      "regime — where a curve, if ingested, would typically be inverting.",
            action="Research focus on producers positioned for physical scarcity (upstream energy, "
                   "specific industrial commodity miners) rather than downstream consumers. Do not "
                   "size on this alone: ingest a real futures-curve feed before treating carry "
                   "inversion as a confirmed signal.",
            themes=["energy", "materials"],
        ))

    # --- 5. Quiet tape (the honest null) --------------------------------------------------------
    if not out:
        out.append(Thesis(
            name="No notable configuration",
            confidence="high",
            evidence=[f"strategic risk {snap.strategic_risk:.0f}", f"VIX {_fmt(vix)}",
                      f"fear/greed {_fmt(fg)}",
                      f"max event acceleration {max(accel.values(), default=1.0):.1f}x"],
            inference="No divergence or concentration worth acting on. A quiet reading is a real "
                      "result, not an absence of one.",
            action="No intel-driven change in posture. Let the validated strategy layer run.",
        ))
    return out


def implicated_symbols(theses: list[Thesis]) -> dict[str, list[str]]:
    """Theme -> exemplar tickers across all theses, for intersecting with validated candidates."""
    out: dict[str, list[str]] = {}
    for t in theses:
        for theme in t.themes:
            out.setdefault(theme, [])
            for s in THEME_EXEMPLARS.get(theme, ()):
                if s not in out[theme]:
                    out[theme].append(s)
    return out
