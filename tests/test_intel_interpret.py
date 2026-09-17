from __future__ import annotations

from trading_live_claude.intel.interpret import (
    CONFLICT_EVENTS_ELEVATED,
    STRATEGIC_RISK_STRESSED,
    implicated_symbols,
    interpret,
)
from trading_live_claude.intel.overlay import IntelSnapshot


def _names(theses) -> set[str]:
    return {t.name for t in theses}


def test_quiet_tape_yields_the_honest_null() -> None:
    """A calm world must not manufacture a thesis out of noise."""
    th = interpret(IntelSnapshot(strategic_risk=30.0, fear_greed=50.0,
                                 market={"equity_vol": 20.0}, event_acceleration={"energy": 1.0}))
    assert len(th) == 1
    assert th[0].name == "No notable configuration"
    assert th[0].themes == []


def test_complacency_divergence_needs_both_sides() -> None:
    """The thesis is a DIVERGENCE: a stressed world alone, or a calm market alone, is not enough."""
    calm_and_stressed = interpret(IntelSnapshot(strategic_risk=69.0, fear_greed=68.0,
                                                market={"equity_vol": 14.4},
                                                event_acceleration={"energy": 6.3}))
    assert "Complacency divergence" in _names(calm_and_stressed)

    # stressed world, but the market is ALSO stressed -> no divergence
    both_stressed = interpret(IntelSnapshot(strategic_risk=69.0, fear_greed=20.0,
                                            market={"equity_vol": 35.0},
                                            event_acceleration={"energy": 6.3}))
    assert "Complacency divergence" not in _names(both_stressed)


def test_energy_concentration_is_flagged_and_scaled_by_magnitude() -> None:
    hot = interpret(IntelSnapshot(event_acceleration={"energy": 6.3, "conflict": 1.0},
                                  energy_stress=0.29, market={"equity_vol": 25.0}))
    energy = [t for t in hot if t.name == "Energy event concentration"]
    assert energy and energy[0].confidence == "high"
    assert "energy" in energy[0].themes
    assert "XLE" in energy[0].exemplars()

    mild = interpret(IntelSnapshot(event_acceleration={"energy": 2.2}, market={"equity_vol": 25.0}))
    e2 = [t for t in mild if t.name == "Energy event concentration"]
    assert e2 and e2[0].confidence == "moderate"   # same thesis, weaker claim


def test_conflict_watch_is_tentative_without_corroborating_flow() -> None:
    # 6 is the calibrated elevated-count gate (2026-09-16); 7 is the moderate cutoff, so a bare
    # 6 with flat flow is the tentative band. Was 4 before the calibration raised the gate.
    th = interpret(IntelSnapshot(conflict_events_active=6, market={"equity_vol": 25.0},
                                 event_acceleration={"conflict": 1.0}))
    c = [t for t in th if t.name == "Conflict escalation watch"]
    assert c and c[0].confidence == "tentative"


def test_sentiment_stretch_reads_both_extremes() -> None:
    greed = interpret(IntelSnapshot(fear_greed=78.0, market={"equity_vol": 25.0}))
    fear = interpret(IntelSnapshot(fear_greed=12.0, market={"equity_vol": 25.0}))
    assert any("greed" in t.name for t in greed)
    assert any("fear" in t.name for t in fear)


def test_no_thesis_is_ever_phrased_as_an_entry_signal() -> None:
    """Intel yields posture and research focus — never 'buy'. Guards the honest framing."""
    snaps = [
        IntelSnapshot(strategic_risk=69.0, fear_greed=68.0, market={"equity_vol": 14.0},
                      event_acceleration={"energy": 6.3}),
        IntelSnapshot(conflict_events_active=6, event_acceleration={"conflict": 3.0}),
        IntelSnapshot(fear_greed=12.0),
    ]
    banned = ("buy ", "sell ", "go long", "short the", "enter ")
    for s in snaps:
        for t in interpret(s):
            assert not any(b in t.action.lower() for b in banned), t.action


def test_implicated_symbols_dedupes_across_theses() -> None:
    th = interpret(IntelSnapshot(strategic_risk=69.0, fear_greed=68.0,
                                 market={"equity_vol": 14.4},
                                 event_acceleration={"energy": 6.3}, conflict_events_active=5))
    imp = implicated_symbols(th)
    assert "safe_haven" in imp                      # cited by more than one thesis
    assert len(imp["safe_haven"]) == len(set(imp["safe_haven"]))


# --- item 4 catalog: three new motifs ------------------------------------------------------------

def test_dollar_strength_divergence_fires_on_the_primary_case() -> None:
    """Strong-and-rising USD alongside a rallying risk asset (crypto or accelerating energy)."""
    th = interpret(IntelSnapshot(market={"equity_vol": 20.0, "dxy_chg": 0.6, "crypto_chg": 2.5},
                                 event_acceleration={"energy": 1.0}))
    names = _names(th)
    assert any("Dollar strength divergence" in n for n in names)
    dollar = [t for t in th if "Dollar strength" in t.name][0]
    # theme mapping must reach the currency AND at least one of the risk-side sectors it implicates
    assert "dollar" in dollar.themes
    assert "emerging_markets" in dollar.themes


def test_dollar_weakness_divergence_fires_when_commodities_ignore_a_weaker_usd() -> None:
    """The mirror case: weak dollar + commodity weakness => demand, not currency."""
    th = interpret(IntelSnapshot(market={"equity_vol": 20.0, "dxy_chg": -0.6},
                                 energy_stress=0.1,
                                 event_acceleration={"energy": 1.0}))
    assert any("Dollar weakness divergence" in n for n in _names(th))


def test_dollar_divergence_stays_silent_when_move_is_within_the_noise_band() -> None:
    """A ±0.4% band exists on purpose — a 0.1% move should not fire this thesis."""
    th = interpret(IntelSnapshot(market={"equity_vol": 20.0, "dxy_chg": 0.1, "crypto_chg": 3.0}))
    assert not any("Dollar" in n for n in _names(th))


def test_disaster_insurance_underpricing_needs_both_disasters_and_calm_market() -> None:
    """Fires only on the divergence — disasters elevated AND accelerating AND VIX calm."""
    fires = interpret(IntelSnapshot(natural_disasters_active=9,
                                    event_acceleration={"disaster": 3.2},
                                    market={"equity_vol": 15.0}))
    assert any("Disaster / insurance underpricing" in n for n in _names(fires))
    t = [x for x in fires if "Disaster / insurance" in x.name][0]
    assert "insurance" in t.themes                       # new theme key wired
    assert "IFC.TO" in t.exemplars() or "RE" in t.exemplars()
    # Not a broad-market call: defense_geopolitical must NOT be attached here.
    assert "defense_geopolitical" not in t.themes

    # Same disasters, but VIX is expensive -> no divergence -> no fire.
    no_fire = interpret(IntelSnapshot(natural_disasters_active=9,
                                      event_acceleration={"disaster": 3.2},
                                      market={"equity_vol": 30.0}))
    assert not any("Disaster / insurance" in n for n in _names(no_fire))


def test_commodity_carry_inversion_proxy_is_capped_at_moderate_confidence() -> None:
    """A proxy without real curve data must never fire as high confidence."""
    th = interpret(IntelSnapshot(energy_stress=0.6,
                                 event_acceleration={"energy": 4.0},
                                 market={"equity_vol": 25.0}))
    carry = [t for t in th if "Commodity carry-inversion" in t.name]
    assert carry and carry[0].confidence == "moderate"
    # The action must explicitly ask for the real feed before treating this as confirmed.
    assert "futures-curve" in carry[0].action.lower() or "curve" in carry[0].action.lower()


def test_carry_inversion_does_not_fire_on_stress_alone_or_flow_alone() -> None:
    """Both conditions must clear together — one on its own is only the energy-concentration read."""
    stress_only = interpret(IntelSnapshot(energy_stress=0.7,
                                          event_acceleration={"energy": 1.2},
                                          market={"equity_vol": 25.0}))
    flow_only = interpret(IntelSnapshot(energy_stress=0.2,
                                        event_acceleration={"energy": 4.0},
                                        market={"equity_vol": 25.0}))
    assert not any("Commodity carry-inversion" in n for n in _names(stress_only))
    assert not any("Commodity carry-inversion" in n for n in _names(flow_only))


# --- agent-layer merge into interpret() ----------------------------------------------------------

import json                             # noqa: E402  (test helpers only, keeps prod imports clean)

import httpx                             # noqa: E402
import pytest                            # noqa: E402
import respx                             # noqa: E402

from trading_live_claude.intel.interpret import enrich_with_agents         # noqa: E402

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


@pytest.fixture()
def _fake_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_live_claude.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")


def _api(text: str) -> httpx.Response:
    return httpx.Response(200, json={"id": "m", "type": "message", "role": "assistant",
                                       "model": "x", "stop_reason": "end_turn",
                                       "content": [{"type": "text", "text": text}]})


def _claim(**k: object) -> str:
    p = {"thesis": "Supply shock", "direction": "constructive", "confidence": 0.7,
          "inference": "Refinery outage reduces distillate supply.",
          "evidence": ["Reuters: outage confirmed"]}
    p.update(k)
    return json.dumps(p)


def _crit(v: str, reason: str = "ok", demote: float = 0.0) -> str:
    return json.dumps({"verdict": v, "reason": reason, "demote_by": demote})


@respx.mock
def test_enrich_prepends_upheld_agent_theses_to_the_rule_reads(_fake_key: None) -> None:
    """An UPHELD specialist reading is added to the rule output, marked so it is visible."""
    calls = iter([_api(_claim()), _api(_crit("UPHELD", "Two sources agree.")),])
    respx.post(_ANTHROPIC_URL).mock(side_effect=lambda req: next(calls))

    # Start with a quiet-tape thesis from the rule layer.
    rule_reads = interpret(IntelSnapshot(strategic_risk=30.0, fear_greed=50.0,
                                          market={"equity_vol": 20.0}))
    assert len(rule_reads) == 1 and rule_reads[0].name == "No notable configuration"

    merged = enrich_with_agents(rule_reads, evidence=[
        {"id": "EV-1", "title": "energy refinery outage", "sources": ["Reuters"]}])
    # Agent thesis is prepended, and the quiet-tape null is preserved so an empty list can never
    # be silently mistaken for interpreter failure.
    assert any(t.name.startswith("[agent · energy]") for t in merged)
    assert any(t.name == "No notable configuration" for t in merged)


@respx.mock
def test_enrich_drops_the_null_when_a_real_rule_thesis_is_also_present(_fake_key: None) -> None:
    """Only prepend the null when the rule read was ONLY the null — otherwise the real rule reads
    speak for themselves and the null is not needed."""
    calls = iter([_api(_claim()), _api(_crit("UPHELD"))])
    respx.post(_ANTHROPIC_URL).mock(side_effect=lambda req: next(calls))

    rule_reads = interpret(IntelSnapshot(
        strategic_risk=69.0, fear_greed=68.0, market={"equity_vol": 14.4},
        event_acceleration={"energy": 6.3}))
    assert any(t.name == "Complacency divergence" for t in rule_reads)

    merged = enrich_with_agents(rule_reads, evidence=[
        {"id": "EV", "title": "energy refinery outage", "sources": ["Reuters"]}])
    # Agent thesis prepended AND every rule thesis kept.
    assert merged[0].name.startswith("[agent")
    for t in rule_reads:
        assert any(m.name == t.name for m in merged)


@respx.mock
def test_enrich_returns_rule_reads_unchanged_when_debate_produces_nothing(_fake_key: None) -> None:
    """FALSIFIED verdict drops the specialist's claim → nothing new to merge → rule reads stand."""
    calls = iter([_api(_claim()), _api(_crit("FALSIFIED", "bundle contradicts the claim"))])
    respx.post(_ANTHROPIC_URL).mock(side_effect=lambda req: next(calls))

    rule_reads = interpret(IntelSnapshot(strategic_risk=30.0, fear_greed=50.0,
                                          market={"equity_vol": 20.0}))
    merged = enrich_with_agents(rule_reads, evidence=[
        {"id": "EV", "title": "energy story", "sources": ["Reuters"]}])
    assert [t.name for t in merged] == [t.name for t in rule_reads]


@respx.mock
def test_enrich_never_raises_on_api_failure(_fake_key: None) -> None:
    """A 500 must not crash interpret() — return rule reads unchanged."""
    respx.post(_ANTHROPIC_URL).mock(return_value=httpx.Response(500, json={"error": "x"}))
    rule_reads = interpret(IntelSnapshot(strategic_risk=30.0))
    merged = enrich_with_agents(rule_reads, evidence=[
        {"id": "EV", "title": "energy story", "sources": ["Reuters"]}])
    assert [t.name for t in merged] == [t.name for t in rule_reads]


@respx.mock
def test_enrich_confidence_bands_map_to_the_rule_layer_vocabulary(_fake_key: None) -> None:
    """0.7+ → high; 0.4..0.7 → moderate; <0.4 → tentative. One vocabulary across both layers."""
    # WEAK demotes 0.5 - so 0.7 becomes 0.2 → tentative.
    calls = iter([_api(_claim(confidence=0.7)), _api(_crit("WEAK", "single source", 0.5))])
    respx.post(_ANTHROPIC_URL).mock(side_effect=lambda req: next(calls))
    merged = enrich_with_agents([], evidence=[
        {"id": "EV", "title": "energy story", "sources": ["Reuters"]}], as_of="now")
    agent = [t for t in merged if t.name.startswith("[agent")]
    assert agent and agent[0].confidence == "tentative"


@respx.mock
def test_agent_thesis_action_is_never_an_entry_signal(_fake_key: None) -> None:
    """Same non-negotiable as the rule layer."""
    calls = iter([_api(_claim()), _api(_crit("UPHELD"))])
    respx.post(_ANTHROPIC_URL).mock(side_effect=lambda req: next(calls))
    merged = enrich_with_agents([], evidence=[
        {"id": "EV", "title": "energy story", "sources": ["Reuters"]}])
    banned = ("buy ", "sell ", "go long", "short the", "enter ")
    for t in merged:
        assert not any(b in t.action.lower() for b in banned), t.action


def test_none_of_the_new_theses_phrase_actions_as_entry_signals() -> None:
    """Same guard as the pre-existing rules — hypotheses only, never 'buy'."""
    snaps = [
        IntelSnapshot(market={"equity_vol": 20.0, "dxy_chg": 0.6, "crypto_chg": 2.5}),
        IntelSnapshot(natural_disasters_active=9, event_acceleration={"disaster": 3.2},
                      market={"equity_vol": 15.0}),
        IntelSnapshot(energy_stress=0.6, event_acceleration={"energy": 4.0},
                      market={"equity_vol": 25.0}),
    ]
    banned = ("buy ", "sell ", "go long", "short the", "enter ")
    for s in snaps:
        for t in interpret(s):
            assert not any(b in t.action.lower() for b in banned), t.action


# ---------------------------------------------------------------------------
# Threshold calibration regression — 2026-09-16
#
# The two gates below were sited beneath their own input's 25th percentile in the
# 280-snapshot corpus, firing on ~75% / ~71% of reads. These tests pin the calibrated
# values and the boundary behaviour so an accidental edit fails loudly rather than
# silently returning the module to a near-always-on base rate.
#
# Boundary-based on purpose: the corpus (state/intel_overlay.jsonl) is gitignored, so a
# test that replayed it would pass locally and skip in CI. Asserting the edges of each
# gate is equivalent for regression purposes and runs anywhere.
# ---------------------------------------------------------------------------


def test_calibrated_thresholds_have_not_drifted() -> None:
    """Pin the constants. If a recalibration moves them, it must move this test too —
    which forces the new base rates to be measured rather than guessed."""
    assert STRATEGIC_RISK_STRESSED == 73.0
    assert CONFLICT_EVENTS_ELEVATED == 6


def _calm_snap(**kw) -> IntelSnapshot:
    """A calm market with no other stressor, so only the gate under test can fire."""
    base = dict(market={"equity_vol": 15.0}, fear_greed=55.0,
                event_acceleration={"energy": 1.0, "conflict": 1.0, "military": 1.0})
    base.update(kw)
    return IntelSnapshot(**base)


def test_strategic_risk_gate_boundary() -> None:
    """Just below the calibrated gate must NOT fire; at the gate must fire."""
    below = interpret(_calm_snap(strategic_risk=STRATEGIC_RISK_STRESSED - 0.1))
    assert "Complacency divergence" not in _names(below)
    assert "No notable configuration" in _names(below)

    at = interpret(_calm_snap(strategic_risk=STRATEGIC_RISK_STRESSED))
    assert "Complacency divergence" in _names(at)


def test_strategic_risk_gate_rejects_the_old_threshold() -> None:
    """The pre-calibration corpus median (70) and old gate (60) sat inside the firing
    band and drove the 75% base rate. Both must now be silent."""
    for sr in (60.0, 65.0, 70.0, 72.0):
        th = interpret(_calm_snap(strategic_risk=sr))
        assert "Complacency divergence" not in _names(th), f"sr={sr} should not fire"


def test_conflict_events_gate_boundary() -> None:
    """5 was the corpus-median neighbourhood and must be silent; 6 fires."""
    below = interpret(_calm_snap(conflict_events_active=CONFLICT_EVENTS_ELEVATED - 1))
    assert "Conflict escalation watch" not in _names(below)

    at = interpret(_calm_snap(conflict_events_active=CONFLICT_EVENTS_ELEVATED))
    assert "Conflict escalation watch" in _names(at)


def test_conflict_events_gate_rejects_the_old_threshold() -> None:
    """The old gate was >=3 against a corpus median of 4 — 71% base rate."""
    for cea in (3, 4, 5):
        th = interpret(_calm_snap(conflict_events_active=cea))
        assert "Conflict escalation watch" not in _names(th), f"cea={cea} should not fire"


def test_conflict_confidence_band_is_not_degenerate() -> None:
    """With the gate at 6, a moderate cutoff of 5 would make every fire moderate and
    collapse the tentative band. 7 keeps both bands reachable."""
    tentative = interpret(_calm_snap(conflict_events_active=6))
    c = [t for t in tentative if t.name == "Conflict escalation watch"]
    assert c and c[0].confidence == "tentative"

    moderate = interpret(_calm_snap(conflict_events_active=7))
    c = [t for t in moderate if t.name == "Conflict escalation watch"]
    assert c and c[0].confidence == "moderate"


def test_complacency_high_confidence_is_reachable() -> None:
    """The old ``sr >= 65 and ea >= 3.0`` conjunct produced 0 of 59 fires at high
    confidence once the main gate moved to 73 — sr and energy flow never spiked together.
    Keying ``high`` on energy acceleration alone keeps the band reachable."""
    high = interpret(_calm_snap(strategic_risk=73.0, event_acceleration={"energy": 3.5}))
    c = [t for t in high if t.name == "Complacency divergence"]
    assert c and c[0].confidence == "high"

    moderate = interpret(_calm_snap(strategic_risk=73.0, event_acceleration={"energy": 1.0}))
    c = [t for t in moderate if t.name == "Complacency divergence"]
    assert c and c[0].confidence == "moderate"


def test_energy_leg_still_fires_complacency_independently_of_strategic_risk() -> None:
    """``stressed_world`` is a disjunction: raising the strategic-risk gate must not break
    the energy-acceleration path into the thesis."""
    th = interpret(_calm_snap(strategic_risk=0.0, event_acceleration={"energy": 2.5}))
    assert "Complacency divergence" in _names(th)
