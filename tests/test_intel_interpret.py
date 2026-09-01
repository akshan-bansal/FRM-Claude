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
