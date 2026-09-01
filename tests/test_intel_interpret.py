from __future__ import annotations

from trading_live_claude.intel.interpret import implicated_symbols, interpret
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
    th = interpret(IntelSnapshot(conflict_events_active=4, market={"equity_vol": 25.0},
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
