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

Threshold calibration — 2026-09-16
----------------------------------
Thresholds were measured against the accrued snapshot corpus (``state/intel_overlay.jsonl``,
280 reads spanning 2026-08-29 → 2026-09-16, zero degraded) by replaying :func:`interpret` over
every snapshot and counting fires. Two gates were sited below their own input's 25th percentile
and were therefore firing on ~3/4 of all reads — a thesis that fires 75% of the time carries
almost no information, which is precisely the "inventing a thesis from noise" failure this module
set out to avoid:

===========================  =========  ==========  =========  ==========
gate                         was        base rate   now        base rate
===========================  =========  ==========  =========  ==========
``strategic_risk``           >= 60.0        75.0%   >= 73.0        21.1%
``conflict_events_active``   >= 3           71.4%   >= 6           17.1%
===========================  =========  ==========  =========  ==========

Post-calibration basket: ``No notable configuration`` 64.6%, ``Complacency divergence`` 21.1%,
``Conflict escalation watch`` 17.1%, ``Energy event concentration`` 1.8%; mean 1.05 theses per
snapshot (was 1.71). The quiet-tape null becoming the dominant read is the honest outcome for a
corpus whose geopolitical index barely moves (``strategic_risk`` p25=65, p50=70, p90=73, p95=74).

**Gates that have NEVER been satisfiable on observed data.** These are retained deliberately —
18 days of one regime is not evidence that an input can never move — but do not expect them to
fire, and do not treat their silence as a signal:

* ``conflict_accel >= 2.0`` / ``>= 2.5`` (theses 1 and 3) — observed max 1.20.
* ``fear_greed >= 70`` / ``<= 25`` (Sentiment stretch) — observed range 54-68. This thesis has
  fired zero times in 280 reads.
* ``natural_disasters_active >= 5`` (Disaster / insurance) — observed max 2. Zero fires.
* ``disaster_accel`` — the ``disaster`` key is **absent** from every observed
  ``event_acceleration`` payload (only ``conflict`` / ``energy`` / ``military`` appear), so this
  leg cannot evaluate regardless of threshold.

``energy_stress >= 0.3`` in thesis 2 is redundant on this corpus: every stress-triggered fire is
already an ``energy_accel >= 2.0`` fire. Kept because it is not *logically* redundant.

**Market-data inputs and their IB availability.** Of the four market fields read here, IB can
serve one properly: ``equity_vol`` (VIX) via ``Index("VIX", "CBOE")`` — worth substituting to
lift coverage from the OSINT-relayed 77% to ~100% and make the gate real-time. ``fear_greed`` has
no IB product (it is a third-party composite). ``dxy_chg`` is only present on 1% of reads and IB
FX was taken off-policy on 2026-09-14. ``crypto_chg`` (12% coverage) belongs to Kraken. Note that
substituting IB VIX will NOT change any firing rate — both ``calm_market`` legs were already
satisfied most of the time, so the saturation was never on the market side.

**Caveat.** These cutoffs are fitted to one regime. If WorldMonitor recentres its strategic-risk
index the constants go stale silently. A trailing-percentile gate would be regime-robust where a
hard constant is not; that is a design change and is queued rather than done here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from trading_live_claude.intel.overlay import IntelSnapshot

# Calibrated 2026-09-16 against the 280-snapshot corpus — see the module header for the
# measurement and the base rates these produce. Named rather than inline so the regression test
# can assert on them directly and a future recalibration is a one-line change with a visible diff.
STRATEGIC_RISK_STRESSED: float = 73.0     # was 60.0 (corpus p25=65 — the old gate never bound)
CONFLICT_EVENTS_ELEVATED: int = 6         # was 3   (corpus median=4 — same problem)

# Themes an intel domain implicates, as ticker exemplars already present in this project's universe.
# Deliberately small and explicit: these are starting points for research, not a sector database.
THEME_EXEMPLARS: dict[str, tuple[str, ...]] = {
    "energy": ("XLE", "USO", "UNG", "CNQ.TO", "ENB.TO", "GEI.TO", "TA.TO", "FRU.TO", "ARX.TO",
               "/CL", "/MCL", "/NG", "/COIL", "/GOIL"),
    "defense_geopolitical": ("ITA", "LMT", "RTX", "NOC", "GD"),
    "safe_haven": ("GLD", "CGL.TO", "SLV", "PAXG/USD", "XLU", "/GC", "/MGC", "/SI", "/SIL"),
    "volatility_convexity": ("VIXY", "UVXY", "SPY puts"),
    "materials": ("XLB", "FCX", "ABX.TO", "AEM.TO", "TECK.B.TO",
                  "/HG", "/MHG", "/FEF", "/PLT", "/PLTM", "/RSS3", "/TF"),
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
    # ``STRATEGIC_RISK_STRESSED`` was 60.0 until the 2026-09-16 threshold calibration (see the
    # module header) showed it sitting below the corpus p25, which made ``stressed_world``
    # ~always true and drove this thesis to a 75% base rate. 73.0 is the measured p90.
    calm_market = (vix is not None and vix < 18.0) or (fg is not None and fg >= 60.0)
    stressed_world = (snap.strategic_risk >= STRATEGIC_RISK_STRESSED
                      or energy_accel >= 2.0 or conflict_accel >= 2.0)
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
            # ``high`` keyed on energy acceleration ALONE (was ``sr >= 65 and ea >= 3.0``). With
            # the main gate now at sr>=73, an ``sr`` sub-condition is either redundant or — as
            # measured 2026-09-16 — unreachable: sr and energy flow never spiked together in the
            # 280-snapshot corpus, so the old conjunct produced 0 of 59 fires at high confidence.
            # Energy accel is the leg with real dynamic range (0.62-6.33 observed).
            confidence="high" if energy_accel >= 3.0 else "moderate",
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
    # Count gate was >=3 until the 2026-09-16 calibration: the corpus median is 4, so >=3 fired on
    # 71% of reads. 6 is the measured p90. The ``conflict_accel`` leg is retained but has never
    # been satisfiable on observed data (max 1.20 vs a 2.5 cutoff) — see the module header.
    # ``moderate`` cutoff raised 5 -> 7 because with the gate at 6 every fire would otherwise be
    # moderate, collapsing the tentative band entirely (measured: 48/48 moderate at >=5).
    if snap.conflict_events_active >= CONFLICT_EVENTS_ELEVATED or conflict_accel >= 2.5:
        out.append(Thesis(
            name="Conflict escalation watch",
            confidence="moderate" if snap.conflict_events_active >= 7 else "tentative",
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


# Domain → theme mapping for the agent layer. Kept tight so an agent-fired thesis in a domain
# implicates the same tickers the rule layer would for that domain, no more.
_AGENT_DOMAIN_THEMES: dict[str, list[str]] = {
    "energy":      ["energy", "materials"],
    "geopolitics": ["defense_geopolitical", "safe_haven"],
    "macro":       ["dollar", "emerging_markets"],
    "disaster":    ["insurance", "materials"],
}


def _agent_to_thesis(fired: Any) -> Thesis:
    """Fold a :class:`intel.agents.FiredThesis` into the shape ``interpret()`` returns.

    Confidence bands mirror the rule layer's tentative/moderate/high strings, so the merged
    output speaks one vocabulary. Action text is deliberately generic ("research focus in the X
    domain; hypothesis pending walk-forward like any other candidate") because the specific
    action would need domain-specific reasoning we do not want to hard-code — the specialist's
    inference already carries the substantive read.
    """
    c = float(fired.confidence)
    band = "high" if c >= 0.7 else "moderate" if c >= 0.4 else "tentative"
    name = f"[agent · {fired.domain}] {fired.thesis}"
    inference = f"{fired.inference}  (adversary: {fired.adversary_verdict}: {fired.adversary_reason})"
    action = (f"Research focus in the {fired.domain} domain; the agent debate upheld this "
              f"reading against the same evidence bundle. Hypothesis pending walk-forward like "
              f"any other candidate — never an entry signal on its own.")
    themes = list(_AGENT_DOMAIN_THEMES.get(fired.domain, []))
    return Thesis(name=name, confidence=band, evidence=list(fired.evidence),
                  inference=inference, action=action, themes=themes)


def enrich_with_agents(
    theses: list[Thesis],
    *,
    evidence: list[dict[str, Any]],
    as_of: str = "",
    client: httpx.Client | None = None,
) -> list[Thesis]:
    """Run the specialist-reader + adversary debate over ``evidence`` and merge the survivors.

    Off the hot path. This adds one LLM round-trip per configured domain plus one adversary call
    per surviving specialist — call from an off-cadence enrichment step, not every poll. Any
    failure inside the agent layer returns the rule-based theses unchanged; nothing about the
    live loop should ever depend on the model being reachable.

    The quiet-tape null thesis is preserved when the rule layer produced it: an empty rule read
    plus an empty debate is still information ("no notable configuration"), and dropping the
    null so an empty list surfaces instead would silently look like the interpreter had failed.
    """
    try:
        from trading_live_claude.intel.agents import debate
    except Exception:                        # pragma: no cover — import guard
        return theses
    try:
        fired = debate(evidence, as_of=as_of, client=client)
    except Exception:
        return theses
    if not fired:
        return theses
    # Prepend agent-fired theses so they appear first, but keep the rule reads.
    non_null = [t for t in theses if t.name != "No notable configuration"]
    agent_theses = [_agent_to_thesis(f) for f in fired]
    return agent_theses + non_null if non_null else agent_theses + theses


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
