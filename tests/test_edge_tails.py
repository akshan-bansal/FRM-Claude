"""Skew and extreme-value fits for graph edge weights (analysis/edge_tails.py).

SYNTHETIC throughout: the samples are drawn here from distributions whose tails are known, to check the
estimator recovers them and refuses what it cannot fit. Nothing here says anything about real edges.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import numpy as np
import pytest

from trading_live_claude.analysis import edge_tails as et
from trading_live_claude.analysis.edge_tails import (
    TailFit,
    TailRefused,
    describe,
    fit_tail,
    skewness,
    tail_report,
    xi_stability,
)
from trading_live_claude.intel.graph import Edge

SRC = Path(__file__).resolve().parent.parent / "src" / "trading_live_claude"


def gpd(n: int, xi: float, sigma: float = 1.0, seed: int = 0) -> np.ndarray:
    return et._draw(np.random.default_rng(seed), n, xi, sigma)


# ---- skew ------------------------------------------------------------------------------------

def test_skew_sign_matches_the_shape() -> None:
    rng = np.random.default_rng(1)
    assert skewness(-rng.exponential(size=5000)) < -1.5            # long tail toward low values
    assert skewness(rng.exponential(size=5000)) > 1.5
    assert abs(skewness(rng.normal(size=5000))) < 0.15
    assert skewness(rng.beta(5, 2, size=5000)) < -0.3              # bounded above, left-skewed


def test_skew_needs_enough_points_and_some_spread() -> None:
    assert skewness([1, 2, 3]) is None and skewness([2.0] * 50) is None


# ---- recovery --------------------------------------------------------------------------------

def test_a_heavy_upper_tail_is_recovered() -> None:
    r = fit_tail(gpd(5000, 0.3), "upper", seed=1)
    assert isinstance(r, TailFit) and r.side == "upper" and r.shape == "heavy"
    assert abs(r.xi - 0.3) < 0.15 and r.xi_ci[0] < r.xi < r.xi_ci[1] and r.n_exceed >= 400


def test_the_lower_tail_reads_the_same_way_and_comes_back_in_original_units() -> None:
    x = -gpd(5000, 0.3)                                             # long tail toward LOW values
    r = fit_tail(x, "lower", seed=1)
    assert isinstance(r, TailFit) and abs(r.xi - 0.3) < 0.15
    assert r.threshold < 0 and r.return_level(2000) < r.threshold   # further out = lower
    assert fit_tail(x, "upper", seed=1).__class__ in (TailFit, TailRefused)


def test_a_bounded_left_skewed_sample_has_a_bounded_upper_tail() -> None:
    x = np.random.default_rng(3).beta(5, 2, size=20000)             # ends at 1: density ~ (1-x)^1 there
    r = fit_tail(x, "upper", seed=1)
    assert isinstance(r, TailFit) and r.shape == "bounded" and -0.8 < r.xi < -0.25
    assert r.threshold + 0 < 1.0 and r.return_level(1e6) < 1.0 + 0.05  # never runs past the endpoint
    low = fit_tail(x, "lower", seed=1)
    assert isinstance(low, TailFit) and low.xi < 0.0                # a bounded lower end too (0)


def test_return_level_agrees_with_the_empirical_quantile() -> None:
    x = gpd(100_000, 0.2, seed=5)
    r = fit_tail(x, "upper", seed=1)
    assert isinstance(r, TailFit)
    emp = float(np.quantile(x, 1 - 1 / 1000))
    assert abs(r.return_level(1000) - emp) / emp < 0.15
    assert r.return_level(1e5) > r.return_level(1e3) > r.threshold


def test_a_return_period_inside_the_sample_is_refused() -> None:
    r = fit_tail(gpd(5000, 0.2), "upper", seed=1)
    assert isinstance(r, TailFit)
    with pytest.raises(ValueError, match="inside the sample"):
        r.return_level(5)


def test_the_goodness_of_fit_accepts_a_true_gpd_and_rejects_a_two_spike_tail() -> None:
    good = fit_tail(gpd(4000, 0.25, seed=2), "upper", seed=1, n_boot=150)
    assert isinstance(good, TailFit) and good.gof_p > 0.05
    rng = np.random.default_rng(4)                       # the tail is two tight spikes, not one GPD
    spikes = np.concatenate([rng.normal(size=3600), 5.0 + 0.1 * rng.normal(size=200),
                             10.0 + 0.1 * rng.normal(size=200)])
    bad = fit_tail(spikes, "upper", seed=1, n_boot=200)
    assert isinstance(bad, TailFit) and bad.gof_p < 0.05


def test_the_goodness_of_fit_p_value_is_calibrated_on_true_tails() -> None:
    """Measured once at 120 replications: 5.0% rejected at the 5% level. Fewer here, so a loose bound."""
    ps = [fit_tail(gpd(3000, 0.25, seed=1000 + i), "upper", seed=i, n_boot=60).gof_p for i in range(40)]  # type: ignore[union-attr]
    assert np.mean(np.array(ps) < 0.05) <= 0.20


def test_the_shape_estimate_is_unbiased_on_a_known_tail() -> None:
    xis = [fit_tail(gpd(3000, 0.25, seed=2000 + i), "upper", seed=i, n_boot=20).xi for i in range(30)]  # type: ignore[union-attr]
    assert abs(float(np.mean(xis)) - 0.25) < 0.05


# ---- refusals --------------------------------------------------------------------------------

def test_too_few_exceedances_is_a_refusal_not_an_estimate() -> None:
    r = fit_tail(gpd(100, 0.3), "upper", seed=1)
    assert isinstance(r, TailRefused) and "only" in r.reason and "need at least" in r.reason


def test_integer_counts_are_refused_as_ties_not_fitted() -> None:
    counts = np.random.default_rng(6).poisson(3, size=4000).astype(float)
    r = fit_tail(counts, "upper", seed=1)
    assert isinstance(r, TailRefused) and "repeat" in r.reason


def test_mass_at_a_clamp_is_not_mistaken_for_a_tail() -> None:
    rng = np.random.default_rng(8)
    infl = np.concatenate([np.full(500, 0.25), np.full(1500, 1.0), 0.25 + 0.75 * rng.beta(5, 2, 2000)])
    r = fit_tail(infl, "lower", seed=1)                               # 12% sit exactly on the floor
    assert isinstance(r, TailRefused)


def test_q_must_be_a_real_quantile() -> None:
    for bad in (0.2, 1.0, 1.5):
        with pytest.raises(ValueError, match="q must be"):
            fit_tail(gpd(1000, 0.2), "upper", seed=1, q=bad)


# ---- randomness is explicit ------------------------------------------------------------------

def test_the_seed_is_required_and_makes_the_result_reproducible() -> None:
    x = gpd(3000, 0.2)
    with pytest.raises(TypeError):
        fit_tail(x, "upper")                                          # type: ignore[call-arg]
    a, b = fit_tail(x, "upper", seed=11), fit_tail(x, "upper", seed=11)
    assert isinstance(a, TailFit) and a == b


def test_xi_is_stable_across_thresholds_for_a_true_gpd() -> None:
    st = xi_stability(gpd(20000, 0.25, seed=9), "upper", seed=1)
    assert len(st) == 4
    xis = [xi for _, _, xi in st]
    assert max(xis) - min(xis) < 0.2


# ---- from edges ------------------------------------------------------------------------------

def _edges(predicate: str, values: np.ndarray, *, as_influence: bool = False) -> list[Edge]:
    return [Edge(("poll", f"p{i}"), predicate, ("symbol", "BTC/USD"),   # type: ignore[arg-type]
                 weight=float(v), influence=float(v) if as_influence else None)
            for i, v in enumerate(values)]


def test_a_report_for_an_edge_family_has_skew_both_tails_and_words() -> None:
    edges = _edges("engaged", gpd(3000, 0.3, seed=12) + 1.0)
    r = tail_report(edges, "engaged", seed=1)
    assert r.predicate == "engaged" and r.measure == "weight" and r.n == 3000
    assert r.skew is not None and r.skew > 1.0
    assert isinstance(r.upper, TailFit) and set(r.xi_stability) == {"lower", "upper"}
    text = describe(r)
    assert "engaged.weight" in text and "right-skewed" in text and "heavy" in text


def test_influence_uses_the_influence_field_and_skips_edges_without_one() -> None:
    rng = np.random.default_rng(13)
    vals = 0.25 + 0.75 * rng.beta(5, 2, size=3000)
    edges = _edges("scaled", vals, as_influence=True) + _edges("scaled", np.array([0.5] * 5))
    r = tail_report(edges, "scaled", seed=1, field_name="influence")
    assert r.n == 3000 and r.measure == "influence" and r.skew is not None and r.skew < 0
    assert "left-skewed" in describe(r)


def test_an_unknown_predicate_is_an_empty_report_not_a_crash() -> None:
    r = tail_report([], "nothing", seed=1)
    assert r.n == 0 and r.skew is None and isinstance(r.lower, TailRefused)
    assert "too few points" in describe(r)


# ---- it is a ruler: nothing that trades may depend on it -------------------------------------

def test_edge_tails_imports_nothing_that_trades() -> None:
    src = (SRC / "analysis" / "edge_tails.py").read_text(encoding="utf-8")
    imports = [ln for ln in src.splitlines() if re.match(r"\s*(from|import)\s", ln)]
    forbidden = ("execution", "risk", "brokers", "strategies", "monitor", "daemon")
    assert not [ln for ln in imports if any(f".{w}" in ln or f" {w}" in ln.split("import")[0] for w in forbidden)]


def _imports(path: Path) -> set[str]:
    """Every module name a file really imports (docstrings and comments do not count)."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.add(base)
            names.update(f"{base}.{a.name}" for a in node.names)
    return names


def test_nothing_outside_analysis_imports_edge_tails() -> None:
    offenders = []
    for f in SRC.rglob("*.py"):
        if "analysis" in f.parts:
            continue
        if any(n.endswith("edge_tails") or ".edge_tails." in n for n in _imports(f)):
            offenders.append(str(f.relative_to(SRC)))
    assert offenders == []
