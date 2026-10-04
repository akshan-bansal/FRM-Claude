"""Society -> graph -> influence -> cost (HOMOGENEOUS_GRAPH_SCOPE.md).

Everything here is SYNTHETIC: seeded RNG and hand-built results. These tests prove wiring and
invariants, not that any simulated stance is right or that the adapter adds value.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from trading_live_claude.intel.graph import (
    DEFAULT_POLICIES,
    Edge,
    append_edges,
    fill_edge,
    load_edges,
    scaled_edge,
    snapshot_to_edges,
    wash_edges,
)
from trading_live_claude.intel.overlay import IntelSnapshot
from trading_live_claude.sim import (
    AdapterParams,
    RunBudget,
    SimResult,
    SimSeed,
    compose,
    cost_check_after_scaling,
    society_edges,
    society_influence,
    society_scaled_edge,
    top_eigenvalue_share,
)
from trading_live_claude.sim.contract import Stance

# TEST VALUES ONLY. The real adapter parameters are a modelling decision, frozen a priori.
P = AdapterParams(a=0.5, b=0.3, c=0.2, d_ref=0.5, floor=0.25)
AS_OF = "2026-09-30T12:00:00+00:00"


def _seed(run_id: str = "abc123def456") -> SimSeed:
    return SimSeed(run_id=run_id, created_at=AS_OF, as_of=AS_OF,
                   symbols=["BTC/USD", "ETH/USD"], snapshot={"strategic_risk": 70.0}, withheld={},
                   budget=RunBudget(max_usd=1, max_agents=5, max_steps=2))


def _result(run_id: str = "abc123def456", stances: list[Stance] | None = None) -> SimResult:
    return SimResult(run_id=run_id, finished_at=AS_OF, completed_steps=2, agents=5,
                     model="claude-haiku-4-5-20251001", spend_usd=0.1, input_tokens=1,
                     output_tokens=1, stopped_by="steps",
                     stances=stances or [Stance(symbol="BTC/USD", mean=-0.4, dispersion=0.3, agents=5),
                                         Stance(symbol="ETH/USD", mean=0.5, dispersion=0.1, agents=5)])


# ---- adapter ---------------------------------------------------------------------------------

def test_params_have_no_defaults() -> None:
    with pytest.raises(TypeError):
        AdapterParams()                                                # type: ignore[call-arg]


@pytest.mark.parametrize("bad", [dict(a=-0.1), dict(b=float("nan")), dict(d_ref=0.0),
                                 dict(floor=0.0), dict(floor=1.5)])
def test_params_reject_nonsense(bad: dict[str, float]) -> None:
    kw = dict(a=0.5, b=0.3, c=0.2, d_ref=0.5, floor=0.25) | bad
    with pytest.raises(ValueError):
        AdapterParams(**kw)


def test_unanimously_constructive_unherded_society_changes_nothing() -> None:
    assert society_influence(mean=1.0, dispersion=0.0, herd=None, params=P) == 1.0
    assert society_influence(mean=0.6, dispersion=0.0, herd=0.0, params=P) == 1.0


def test_influence_never_exceeds_one_over_a_dense_grid() -> None:
    for mean in np.linspace(-1.5, 1.5, 31):                 # includes out-of-contract means
        for disp in (0.0, 0.1, 0.5, 5.0):
            for herd in (None, 0.0, 0.4, 1.0, 7.0):
                v = society_influence(mean=float(mean), dispersion=disp, herd=herd, params=P)
                assert v is not None and P.floor <= v <= 1.0


def test_adverse_consensus_reduces_and_constructive_does_not() -> None:
    adverse = society_influence(mean=-0.8, dispersion=0.0, herd=None, params=P)
    mild = society_influence(mean=-0.2, dispersion=0.0, herd=None, params=P)
    constructive = society_influence(mean=0.8, dispersion=0.0, herd=None, params=P)
    assert adverse is not None and mild is not None and constructive is not None
    assert adverse < mild < constructive == 1.0
    assert adverse == pytest.approx(1.0 - 0.5 * 0.8)


def test_disagreement_and_herding_reduce_even_when_constructive() -> None:
    base = society_influence(mean=0.5, dispersion=0.0, herd=0.0, params=P)
    split = society_influence(mean=0.5, dispersion=0.5, herd=0.0, params=P)
    herded = society_influence(mean=0.5, dispersion=0.0, herd=1.0, params=P)
    assert base == 1.0 and split is not None and herded is not None
    assert split == pytest.approx(0.7) and herded == pytest.approx(0.8)


def test_floor_holds_and_nonfinite_inputs_carry_no_view() -> None:
    harsh = AdapterParams(a=5.0, b=5.0, c=5.0, d_ref=0.1, floor=0.3)
    assert society_influence(mean=-1.0, dispersion=1.0, herd=1.0, params=harsh) == 0.3
    assert society_influence(mean=float("nan"), dispersion=0.1, herd=None, params=P) is None
    assert society_influence(mean=0.1, dispersion=float("inf"), herd=None, params=P) is None


def test_compose_is_one_clamped_product_and_skips_missing_factors() -> None:
    assert compose(0.5, 0.8) == pytest.approx(0.4)
    assert compose(None, 0.8) == 0.8 and compose(0.5, None) == 0.5 and compose(None, None) is None
    assert compose(1.4, 1.3) == 1.0                         # never above 1, even from bad inputs


# ---- consensus concentration -----------------------------------------------------------------

def test_top_eigenvalue_share_refuses_a_matrix_it_cannot_support() -> None:
    rng = np.random.default_rng(0)                          # SYNTHETIC
    assert top_eigenvalue_share(rng.normal(size=(5, 6))) is None          # 5 agents: below the floor
    assert top_eigenvalue_share(rng.normal(size=(60, 1))) is None         # one symbol
    bad = rng.normal(size=(60, 4))
    bad[3, 2] = np.nan
    assert top_eigenvalue_share(bad) is None
    assert top_eigenvalue_share(np.ones((60, 4))) is None                 # no spread at all


def test_top_eigenvalue_share_separates_herded_from_independent() -> None:
    rng = np.random.default_rng(1)                          # SYNTHETIC
    common = rng.normal(size=(200, 1))
    herded = common + 0.05 * rng.normal(size=(200, 6))
    independent = rng.normal(size=(200, 6))
    h, i = top_eigenvalue_share(herded), top_eigenvalue_share(independent)
    assert h is not None and i is not None
    assert h > 0.9 and i < 0.4 and 1 / 6 <= i <= h <= 1.0


# ---- edges -----------------------------------------------------------------------------------

def test_society_edges_shape_and_as_of_comes_from_the_seed() -> None:
    edges = society_edges(_seed(), _result())
    assert [e.predicate for e in edges] == ["holds_stance", "holds_stance"]
    e = edges[0]
    assert e.subject == ("society", "abc123def456") and e.object == ("symbol", "BTC/USD")
    assert e.weight == -0.4 and e.as_of == AS_OF and e.influence is None
    assert e.meta["dispersion"] == 0.3 and e.meta["agents"] == 5.0


def test_a_result_from_another_run_is_refused() -> None:
    with pytest.raises(ValueError, match="does not belong"):
        society_edges(_seed("aaaaaaaaaaaa"), _result("bbbbbbbbbbbb"))


def test_society_scaled_edge_records_both_factors_and_the_whole_adapter() -> None:
    st = Stance(symbol="BTC/USD", mean=-0.4, dispersion=0.3, agents=5)
    e = society_scaled_edge(seed=_seed(), stance=st, params=P, overlay_scalar=0.8, herd=None,
                            poll_id="poll-1", asset_class="crypto", intent_id="intent-9",
                            strategy="bollinger")
    assert e is not None
    soc = society_influence(mean=-0.4, dispersion=0.3, herd=None, params=P)
    assert e.predicate == "scaled" and e.subject == ("poll", "poll-1") and e.object == ("symbol", "BTC/USD")
    assert e.influence == pytest.approx(0.8 * soc) and 0.0 < e.influence <= 1.0
    assert e.meta["society_run_id"] == "abc123def456" and e.meta["intent_id"] == "intent-9"
    assert e.meta["overlay_scalar"] == 0.8 and e.meta["society_influence"] == pytest.approx(soc)
    assert {"adapter_a", "adapter_b", "adapter_c", "adapter_d_ref", "adapter_floor"} <= set(e.meta)


def test_no_view_from_either_factor_yields_no_edge() -> None:
    st = Stance(symbol="BTC/USD", mean=0.0, dispersion=0.0, agents=5)
    bad = AdapterParams(a=0.1, b=0.1, c=0.1, d_ref=1.0, floor=0.5)
    assert society_scaled_edge(seed=_seed(), stance=st, params=bad, overlay_scalar=None, herd=None,
                               poll_id="p", asset_class="crypto", intent_id="i",
                               strategy="s") is not None          # society alone still speaks (1.0)
    nan = Stance.model_construct(symbol="X", mean=float("nan"), dispersion=0.0, agents=5)
    assert society_scaled_edge(seed=_seed(), stance=nan, params=bad, overlay_scalar=None, herd=None,
                               poll_id="p", asset_class="crypto", intent_id="i", strategy="s") is None


# ---- the graph is one component --------------------------------------------------------------

def _components(edges: list[Edge]) -> int:
    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(x: tuple[str, str]) -> tuple[str, str]:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in edges:
        parent[find(e.subject)] = find(e.object)
    return len({find(n) for n in list(parent)})


def _world() -> tuple[list[Edge], str]:
    # "geopolitical" must be observed too: the ``market:global --stressed_by--> domain:geopolitical``
    # bridge hangs off no poll, so it joins the main component only through an observed domain. A
    # snapshot that never observes it leaves that pair as its own island (seen while writing this).
    snap = IntelSnapshot(category_alert_counts={"conflict": 4, "economy": 2, "geopolitical": 1},
                         country_alert_counts={"US": 3}, event_acceleration={"conflict": 3.0},
                         strategic_risk=70.0, as_of=datetime(2026, 9, 30, 12, tzinfo=UTC))
    intel = snapshot_to_edges(snap)
    poll_id = snap.as_of.isoformat()
    fill = fill_edge(venue="kraken", symbol="BTC/USD", action="BUY", quantity=0.1, price=60000.0,
                     session_id="s1", order_id=1, as_of=poll_id)
    return [*intel, fill], poll_id


def test_without_the_new_edges_intel_and_execution_are_two_components() -> None:
    edges, _ = _world()
    assert _components(edges) == 2                          # the gap GRAPH_INFLUENCE_SCOPE.md measured


def test_scaled_plus_society_edges_make_one_component() -> None:
    edges, poll_id = _world()
    st = Stance(symbol="BTC/USD", mean=-0.4, dispersion=0.3, agents=5)
    scaled = society_scaled_edge(seed=_seed(), stance=st, params=P, overlay_scalar=0.9, herd=None,
                                 poll_id=poll_id, asset_class="crypto", intent_id="i-1", strategy="b")
    assert scaled is not None
    joined = [*edges, scaled]
    assert _components(joined) == 1                         # scaled joins poll -> symbol <- venue
    assert _components([*joined, *society_edges(_seed(), _result())]) == 1   # society hangs off symbol


def test_scaled_edge_alone_still_joins_the_two_islands() -> None:
    edges, poll_id = _world()
    e = scaled_edge(poll_id=poll_id, symbol="BTC/USD", scalar=0.7, asset_class="crypto",
                    intent_id="i-2", strategy="b")
    assert _components([*edges, e]) == 1


# ---- decay and persistence -------------------------------------------------------------------

def test_stance_and_loading_decay_but_scaled_and_traded_do_not() -> None:
    assert "holds_stance" in DEFAULT_POLICIES and "loads_on" in DEFAULT_POLICIES
    assert "scaled" not in DEFAULT_POLICIES and "traded" not in DEFAULT_POLICIES
    now = datetime(2026, 10, 10, tzinfo=UTC)
    old = Edge(("society", "r"), "holds_stance", ("symbol", "BTC/USD"), weight=-0.5,
               as_of=datetime(2026, 9, 30, tzinfo=UTC).isoformat())     # 10 days > 7-day ttl
    kept = Edge(("poll", "p"), "scaled", ("symbol", "BTC/USD"), weight=0.7,
                as_of=datetime(2026, 9, 30, tzinfo=UTC).isoformat(), influence=0.7)
    survivors = wash_edges([old, kept], now=now)
    assert kept in survivors and old not in survivors


def test_wash_keeps_a_fresh_adverse_stance_and_decays_it_toward_zero() -> None:
    """Regression: a signed compare against min_weight deleted every negative-weight edge."""
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)
    adverse = Edge(("society", "r"), "holds_stance", ("symbol", "BTC/USD"), weight=-0.8,
                   as_of=datetime(2026, 9, 30, 12, tzinfo=UTC).isoformat())     # 24h old = 1 half-life
    constructive = Edge(("society", "r"), "holds_stance", ("symbol", "ETH/USD"), weight=0.8,
                        as_of=datetime(2026, 9, 30, 12, tzinfo=UTC).isoformat())
    out = {e.object[1]: e for e in wash_edges([adverse, constructive], now=now)}
    assert set(out) == {"BTC/USD", "ETH/USD"}
    assert out["BTC/USD"].weight == pytest.approx(-0.4) and out["ETH/USD"].weight == pytest.approx(0.4)


def test_journal_roundtrip_keeps_influence_and_meta(tmp_path: Path) -> None:
    st = Stance(symbol="ETH/USD", mean=-0.3, dispersion=0.2, agents=5)
    e = society_scaled_edge(seed=_seed(), stance=st, params=P, overlay_scalar=0.9, herd=0.2,
                            poll_id="p", asset_class="crypto", intent_id="i-3", strategy="s")
    assert e is not None
    path = tmp_path / "intel_graph.jsonl"
    append_edges([*society_edges(_seed(), _result()), e], path=path)
    back = load_edges(path)
    assert back[-1].influence == e.influence and back[-1].meta == e.meta
    assert back[0].predicate == "holds_stance" and back[0].influence is None


# ---- cost after scaling ----------------------------------------------------------------------

def test_scaling_down_raises_the_fixed_fee_ratio_on_questrade() -> None:
    full = cost_check_after_scaling(venue="questrade", shares=100, price=10.0, influence=1.0, max_ratio=0.0)
    half = cost_check_after_scaling(venue="questrade", shares=100, price=10.0, influence=0.5, max_ratio=0.0)
    assert full.ok and half.ok                              # gate off: always passes
    assert half.ratio > full.ratio * 1.5                    # the fee is fixed, the notional halved


def test_an_intent_pushed_over_the_cap_by_scaling_is_rejected_with_the_number() -> None:
    cap = 0.02
    ok = cost_check_after_scaling(venue="questrade", shares=1000, price=10.0, influence=1.0, max_ratio=cap)
    scaled = cost_check_after_scaling(venue="questrade", shares=1000, price=10.0, influence=0.05,
                                      max_ratio=cap)
    assert ok.ok and not scaled.ok
    assert "cap after scaling" in scaled.reason and f"{scaled.ratio:.2%}" in scaled.reason


def test_percentage_fee_venue_ratio_is_size_independent() -> None:
    a = cost_check_after_scaling(venue="kraken", shares=1.0, price=60000.0, influence=1.0, max_ratio=0.0)
    b = cost_check_after_scaling(venue="kraken", shares=1.0, price=60000.0, influence=0.3, max_ratio=0.0)
    assert a.ratio == pytest.approx(b.ratio)
