"""``loads_on`` edges: the envelope, the recorded estimator, decay, the journal round trip, and the
join into the rest of the graph. Worlds are SYNTHETIC."""
from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.analysis import factor_edges
from trading_live_claude.analysis.factor_edges import (
    eigen_factor_edges,
    eigen_factor_id,
    named_factor_edges,
    named_factor_id,
    society_factor_edges,
    society_factor_id,
)
from trading_live_claude.analysis.factors import (
    eigen_factor_model,
    fit_named_factors,
    society_factor,
)
from trading_live_claude.analysis.spectral import MPSpec
from trading_live_claude.intel.graph import (
    Edge,
    append_edges,
    fill_edge,
    load_edges,
    scaled_edge,
    snapshot_to_edges,
    wash_edges,
)
from trading_live_claude.intel.overlay import IntelSnapshot

AS_OF = "2026-10-03T21:00:00+00:00"


def _returns(seed: int = 0, t: int = 500, n: int = 19) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    market = rng.standard_normal((t, 1))
    sector = np.zeros((t, n))
    sector[:, :5] = rng.standard_normal((t, 1))
    return pd.DataFrame(0.9 * market + 0.5 * sector + 0.5 * rng.standard_normal((t, n)),
                        columns=[f"S{i}" for i in range(n)])


# --------------------------------------------------------------------------- eigen


def test_one_edge_per_symbol_per_structural_eigenfactor() -> None:
    model = eigen_factor_model(_returns())
    edges = eigen_factor_edges(model, as_of=AS_OF)
    assert len(edges) == 19 * model.n_signal == 38
    for e in edges:
        assert e.predicate == "loads_on" and e.subject[0] == "symbol" and e.object[0] == "factor"
        assert e.influence is None and e.as_of == AS_OF
        assert all(isinstance(v, float | str) for v in e.meta.values())
    assert {e.object[1] for e in edges} == {eigen_factor_id(1), eigen_factor_id(2)}
    first = [e for e in edges if e.object[1] == "eigen:1"]
    np.testing.assert_allclose([e.weight for e in first], model.loadings[:, 0])
    assert [e.subject[1] for e in first] == list(model.symbols)


def test_every_eigen_edge_records_the_estimator_and_what_shaped_it() -> None:
    model = eigen_factor_model(_returns())
    for e in eigen_factor_edges(model, as_of=AS_OF):
        m = e.meta
        assert m["kind"] == "eigen"
        assert m["mp_estimator"] == "analysis.rmt.denoise_correlation"
        assert (m["mp_tw_sigmas"], m["mp_lower_cut"], m["mp_noise_shape"]) == (2.0, 0.0, "constant")
        assert m["sigma2"] == pytest.approx(model.sigma2)
        assert m["lambda_plus"] == pytest.approx(model.lambda_plus) and m["threshold"] == pytest.approx(model.threshold)
        assert (m["n_obs"], m["n_assets"], m["n_signal"]) == (500.0, 19.0, 2.0)
        assert {"eigenvalue", "variance_share", "factor_rank"} <= set(m)


def test_a_different_spec_shows_up_on_the_edge_it_produced() -> None:
    edges = eigen_factor_edges(eigen_factor_model(_returns(), MPSpec(tw_sigmas=0.0)), as_of=AS_OF)
    assert {e.meta["mp_tw_sigmas"] for e in edges} == {0.0}


def test_no_structure_means_no_edges_not_a_manufactured_one() -> None:
    noise = pd.DataFrame(np.random.default_rng(1).standard_normal((2000, 30)))
    assert eigen_factor_edges(eigen_factor_model(noise), as_of=AS_OF) == []


# --------------------------------------------------------------------------- named and society


def test_named_edges_carry_the_beta_and_its_evidence() -> None:
    rng = np.random.default_rng(2)
    idx = pd.date_range("2024-01-01", periods=400, freq="B", tz="UTC")
    f = pd.DataFrame(rng.standard_normal((400, 2)), index=idx, columns=["MKT", "SMB"])
    r = pd.DataFrame(f.to_numpy() @ rng.uniform(-1, 1, (2, 4)) + 0.4 * rng.standard_normal((400, 4)),
                     index=idx, columns=list("WXYZ"))
    fit = fit_named_factors(r, f)
    edges = named_factor_edges(fit, as_of=AS_OF)
    assert len(edges) == 8 and {e.object[1] for e in edges} == {named_factor_id("MKT"), named_factor_id("SMB")}
    e = next(e for e in edges if e.subject[1] == "X" and e.object[1] == "named:SMB")
    assert e.weight == pytest.approx(fit.betas[1, 1])
    assert e.meta["kind"] == "named" and e.meta["estimator"] == "ols" and e.meta["factor"] == "SMB"
    assert e.meta["t_stat"] == pytest.approx(fit.t_betas[1, 1]) and e.meta["r2"] == pytest.approx(fit.r2[1])
    assert e.meta["n_obs"] == 400.0 and e.influence is None


def _society_edges(as_of: str = AS_OF, run_id: str = "run42") -> list[Edge]:
    rng = np.random.default_rng(3)
    common = rng.standard_normal((200, 1))
    x = np.clip(0.7 * common + 0.3 * rng.standard_normal((200, 5)), -3, 3) / 3
    sf = society_factor(x, ["A", "B", "C", "D", "E"])
    assert sf is not None
    return society_factor_edges(sf, run_id=run_id, as_of=as_of)


def test_society_edges_belong_to_one_run_and_carry_its_concentration() -> None:
    edges = _society_edges()
    assert len(edges) == 5 and {e.object for e in edges} == {("factor", society_factor_id("run42"))}
    m = edges[0].meta
    assert m["kind"] == "society" and m["run_id"] == "run42" and 0.2 <= float(m["share"]) <= 1.0
    assert m["n_agents"] == 200.0 and m["n_symbols"] == 5.0 and m["n_dropped"] == 0.0
    assert all(e.weight > 0 and e.influence is None for e in edges)
    with pytest.raises(ValueError, match="run_id"):
        _society_edges(run_id="")


# --------------------------------------------------------------------------- the data's own time


@pytest.mark.parametrize("bad", ["", "yesterday", "2026-10-03T21:00:00", "2026-10-03"])
def test_as_of_must_be_a_zoned_iso_timestamp(bad: str) -> None:
    with pytest.raises(ValueError, match="as_of"):
        eigen_factor_edges(eigen_factor_model(_returns()), as_of=bad)


def test_a_z_suffix_is_accepted_and_the_stamp_is_kept_verbatim() -> None:
    edges = eigen_factor_edges(eigen_factor_model(_returns()), as_of="2026-10-03T21:00:00Z")
    assert {e.as_of for e in edges} == {"2026-10-03T21:00:00Z"}


def test_the_same_inputs_give_the_same_edges_because_nothing_reads_the_clock() -> None:
    model = eigen_factor_model(_returns())
    assert eigen_factor_edges(model, as_of=AS_OF) == eigen_factor_edges(model, as_of=AS_OF)


def test_a_non_finite_loading_is_an_error_not_an_edge() -> None:
    model = eigen_factor_model(_returns())
    bad = model.loadings.copy()
    bad[3, 0] = np.nan
    with pytest.raises(ValueError, match="not finite"):
        eigen_factor_edges(replace(model, loadings=bad), as_of=AS_OF)
    with pytest.raises(ValueError, match="unique"):
        eigen_factor_edges(replace(model, symbols=("S0",) * 19), as_of=AS_OF)


# --------------------------------------------------------------------------- the real graph


def test_edges_survive_the_journal_round_trip_unchanged(tmp_path: Path) -> None:
    edges = [*eigen_factor_edges(eigen_factor_model(_returns()), as_of=AS_OF), *_society_edges()]
    for e in edges[:5]:
        assert Edge.from_row(json.loads(json.dumps(e.to_row()))) == e
    journal = tmp_path / "graph.jsonl"
    append_edges(edges, journal)
    assert load_edges(journal) == edges


def test_loadings_decay_like_any_loads_on_edge_and_expire() -> None:
    edges = eigen_factor_edges(eigen_factor_model(_returns()), as_of=AS_OF)
    t0 = datetime.fromisoformat(AS_OF)
    half = wash_edges(edges, now=t0 + timedelta(hours=72))  # the policy's half-life
    assert len(half) == len(edges)
    for old, new in zip(edges, half, strict=True):
        assert new.weight == pytest.approx(old.weight * 0.5, rel=1e-9)
    assert wash_edges(edges, now=t0 + timedelta(days=15)) == []  # 14-day TTL


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


def test_factor_edges_hang_off_symbols_and_never_add_an_island() -> None:
    snap = IntelSnapshot(category_alert_counts={"conflict": 4, "economy": 2, "geopolitical": 1},
                         country_alert_counts={"US": 3}, event_acceleration={"conflict": 3.0},
                         strategic_risk=70.0, as_of=datetime(2026, 9, 30, 12, tzinfo=UTC))
    poll_id = snap.as_of.isoformat()
    base = [*snapshot_to_edges(snap),
            fill_edge(venue="questrade", symbol="S0", action="BUY", quantity=10, price=50.0,
                      session_id="s1", order_id=1, as_of=poll_id),
            scaled_edge(poll_id=poll_id, symbol="S0", scalar=0.8, asset_class="equity",
                        intent_id="i-1", strategy="b", as_of=poll_id)]
    assert _components(base) == 1  # intel + fill + scaled: the single component the scope asks for
    # Symbols S0..S18 include the traded one, so the eigen factors attach to it and add no island.
    joined = [*base, *eigen_factor_edges(eigen_factor_model(_returns()), as_of=AS_OF)]
    assert _components(joined) == 1
    # Society edges over symbols the book never touched (A..E) cannot join anything, and say so.
    assert _components([*base, *_society_edges()]) == 2
    assert _components([*joined, *_society_edges()]) == 2


# --------------------------------------------------------------------------- boundary


def test_the_edge_builder_imports_the_graph_and_the_factor_models_only() -> None:
    tree = ast.parse(Path(factor_edges.__file__).read_text(encoding="utf-8"))
    absolute: set[str] = set()
    relative: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            absolute |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            (relative if node.level else absolute).add((node.module or "").split(".")[0])
    assert absolute <= {"__future__", "math", "datetime"}, absolute
    assert relative == {"intel", "factors"}, relative


def test_importing_the_analysis_package_does_not_pull_in_the_graph() -> None:
    init = Path(factor_edges.__file__).with_name("__init__.py").read_text(encoding="utf-8")
    assert "factor_edges" not in init and "intel" not in init
