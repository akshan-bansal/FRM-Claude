"""Named, eigen and society factors. Every world below is SYNTHETIC: it proves the plumbing and the
statistics, and says nothing about real markets or real factor premia."""
from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.analysis import factors
from trading_live_claude.analysis.factors import (
    MIN_AGENTS_FOR_SPECTRUM,
    eigen_factor_model,
    fit_named_factors,
    named_factor_risk,
    society_factor,
)
from trading_live_claude.analysis.rmt import fit_rmt
from trading_live_claude.analysis.spectral import PINNED_MP, MPSpec, eigenfactor_risk


def _world(t: int = 2000, n: int = 6, k: int = 3, seed: int = 0, noise: float = 0.5
           ) -> tuple[pd.DataFrame, pd.DataFrame, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=t, freq="B", tz="UTC")
    f = pd.DataFrame(rng.standard_normal((t, k)), index=idx, columns=["MKT", "SMB", "HML"][:k])
    betas = rng.uniform(-1.0, 1.5, size=(n, k))
    alpha = rng.normal(0.0, 0.01, n)
    eps = rng.standard_normal((t, n)) * noise
    r = pd.DataFrame(alpha + f.to_numpy() @ betas.T + eps, index=idx, columns=[f"A{i}" for i in range(n)])
    return r, f, betas, alpha


# --------------------------------------------------------------------------- named factors


def test_named_loadings_match_statsmodels_ols() -> None:
    sm = pytest.importorskip("statsmodels.api")
    r, f, _, _ = _world(t=400, seed=1)
    fit = fit_named_factors(r, f)
    design = sm.add_constant(f.to_numpy())
    for i, col in enumerate(r.columns):
        ref = sm.OLS(r[col].to_numpy(), design).fit()
        np.testing.assert_allclose(fit.alpha[i], ref.params[0], atol=1e-10)
        np.testing.assert_allclose(fit.betas[i], ref.params[1:], atol=1e-10)
        np.testing.assert_allclose(fit.t_betas[i], ref.tvalues[1:], atol=1e-8)
        assert fit.r2[i] == pytest.approx(ref.rsquared, abs=1e-12)
        assert fit.resid_var[i] == pytest.approx(ref.mse_resid, rel=1e-10)
    assert fit.assets == tuple(r.columns) and fit.factors == ("MKT", "SMB", "HML")
    assert fit.n_obs == 400 and fit.n_dropped == 0


def test_named_loadings_recover_the_planted_betas() -> None:
    r, f, betas, alpha = _world(t=20000, seed=2, noise=0.3)
    fit = fit_named_factors(r, f)
    np.testing.assert_allclose(fit.betas, betas, atol=0.02)
    np.testing.assert_allclose(fit.alpha, alpha, atol=0.01)
    assert (fit.r2 > 0.5).all() and (np.abs(fit.t_betas) > 5).mean() > 0.8


def test_named_fit_refuses_to_guess() -> None:
    r, f, _, _ = _world(t=300, seed=3)
    collinear = f.assign(DUP=f["MKT"] * 2.0 + f["SMB"])
    with pytest.raises(ValueError, match="collinear"):
        fit_named_factors(r, collinear)
    with pytest.raises(ValueError, match="collinear"):
        fit_named_factors(r, f.assign(FLAT=1.0))  # a constant is collinear with the intercept
    with pytest.raises(ValueError, match="same index"):
        fit_named_factors(r, f.iloc[1:])  # no silent inner join
    with pytest.raises(ValueError, match="both an asset and a factor"):
        fit_named_factors(r, f.rename(columns={"MKT": "A0"}))
    with pytest.raises(ValueError, match="at least"):
        fit_named_factors(r.iloc[:4], f.iloc[:4])
    flat = r.copy()
    flat["A1"] = 0.0
    with pytest.raises(ValueError, match="zero variance"):
        fit_named_factors(flat, f)


def test_rows_with_missing_values_are_dropped_and_counted_not_hidden() -> None:
    r, f, _, _ = _world(t=300, seed=4)
    r.iloc[10, 2] = np.nan
    f.iloc[20, 0] = np.inf
    fit = fit_named_factors(r, f)
    assert fit.n_obs == 298 and fit.n_dropped == 2


def test_named_factor_risk_splits_variance_exactly() -> None:
    r, f, _, _ = _world(t=3000, seed=5)
    fit = fit_named_factors(r, f)
    w = np.array([0.3, 0.2, 0.1, -0.1, 0.25, 0.25])
    risk = named_factor_risk(w, fit, sample_cov=np.cov(r.to_numpy(), rowvar=False))
    full = fit.betas @ fit.factor_cov @ fit.betas.T + np.diag(fit.resid_var)
    assert risk.total == pytest.approx(float(w @ full @ w), rel=1e-12)
    assert risk.factor_variance.sum() == pytest.approx(risk.systematic, rel=1e-12)
    assert risk.systematic + risk.idiosyncratic == pytest.approx(risk.total)
    np.testing.assert_allclose(risk.exposure, fit.betas.T @ w)
    assert risk.sample_variance_gap is not None
    sample_var = float(w @ np.cov(r.to_numpy(), rowvar=False) @ w)
    assert abs(risk.sample_variance_gap) / sample_var < 0.05  # residuals are independent by construction
    with pytest.raises(ValueError, match="weights"):
        named_factor_risk(np.ones(3), fit)


# --------------------------------------------------------------------------- eigenfactors


def _structured(seed: int = 0, t: int = 500, n: int = 19) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    market = rng.standard_normal((t, 1))
    sector = np.zeros((t, n))
    sector[:, :5] = rng.standard_normal((t, 1))
    x = 0.9 * market + 0.5 * sector + 0.5 * rng.standard_normal((t, n))
    return pd.DataFrame(x, columns=[f"S{i}" for i in range(n)])


def test_planted_structure_becomes_two_eigenfactors() -> None:
    df = _structured(0)
    m = eigen_factor_model(df)
    assert m.n_signal == 2 and m.loadings.shape == (19, 2) and m.eigenvalues.shape == (2,)
    assert (m.loadings[:, 0] > 0).all()  # the market mode reads "long the basket"
    assert (m.eigenvalues[:-1] >= m.eigenvalues[1:]).all()
    assert m.variance_share.sum() == pytest.approx(m.eigenvalues.sum() / 19)
    assert m.symbols == tuple(df.columns) and m.n_obs == 500 and m.spec == PINNED_MP
    assert m.spectrum.n_structural == m.n_signal == fit_rmt(df).n_signal
    sector = m.loadings[:5, 1], m.loadings[5:, 1]
    assert abs(sector[0].mean() - sector[1].mean()) > 0.2  # the second factor separates the sector out


def test_loadings_are_the_final_matrixs_eigenpairs_and_close_to_rmts_own_embedding() -> None:
    df = _structured(1)
    m = eigen_factor_model(df)
    vals, vecs = np.linalg.eigh(m.clean_corr)
    order = np.argsort(vals)[::-1][:2]
    np.testing.assert_allclose(np.abs(m.loadings), np.abs(vecs[:, order] * np.sqrt(vals[order])), atol=1e-9)
    np.testing.assert_allclose(np.abs(m.loadings), np.abs(fit_rmt(df).embed(2)), atol=0.08)


def test_eigenfactor_variance_reconciles_with_the_filtered_covariance() -> None:
    df = _structured(2)
    m = eigen_factor_model(df)
    w = np.linspace(0.02, 0.15, 19)
    w = w / w.sum()
    res = eigenfactor_risk(m.clean_corr, m.vol, w)
    full = m.clean_corr * np.outer(m.vol, m.vol)
    assert res.total == pytest.approx(float(w @ full @ w), rel=1e-10)
    assert res.share[0] > 0.5  # an all-long book is mostly the market mode


def test_a_window_with_no_structure_yields_no_factors() -> None:
    noise = pd.DataFrame(np.random.default_rng(3).standard_normal((2000, 30)))
    m = eigen_factor_model(noise)
    assert m.n_signal == 0 and m.loadings.shape == (30, 0) and m.eigenvalues.size == 0


def test_k_may_trim_but_never_invent_a_factor() -> None:
    df = _structured(4)
    assert eigen_factor_model(df, k=1).loadings.shape == (19, 1)
    assert eigen_factor_model(df, k=0).loadings.shape == (19, 0)
    with pytest.raises(ValueError, match="structural"):
        eigen_factor_model(df, k=3)


def test_the_spec_in_use_is_the_spec_recorded() -> None:
    df = _structured(5)
    loose = MPSpec(tw_sigmas=0.0)
    m = eigen_factor_model(df, loose)
    assert m.spec == loose and m.threshold == pytest.approx(fit_rmt(df, tw_sigmas=0.0).threshold)
    assert m.sigma2 == pytest.approx(fit_rmt(df, tw_sigmas=0.0).sigma2)


# --------------------------------------------------------------------------- society factor


def _society(agents: int = 200, symbols: int = 6, seed: int = 0, consensus: float = 0.7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    common = rng.standard_normal((agents, 1))
    out: np.ndarray = np.clip(consensus * common + (1.0 - consensus) * rng.standard_normal((agents, symbols)), -3, 3) / 3
    return out


NAMES = [f"SYM{i}" for i in range(6)]


def test_share_agrees_with_the_societys_own_function() -> None:
    from trading_live_claude.sim.society import MIN_AGENTS_FOR_SPECTRUM as SIM_MIN
    from trading_live_claude.sim.society import top_eigenvalue_share

    assert MIN_AGENTS_FOR_SPECTRUM == SIM_MIN  # the two thresholds must move together
    for seed in range(5):
        x = _society(seed=seed, consensus=0.2 + 0.15 * seed)
        sf = society_factor(x, NAMES)
        assert sf is not None and sf.share == pytest.approx(top_eigenvalue_share(x), abs=1e-12)


def test_strong_consensus_gives_high_concentration_and_aligned_loadings() -> None:
    sf = society_factor(_society(consensus=0.9), NAMES)
    weak = society_factor(_society(consensus=0.05), NAMES)
    assert sf is not None and weak is not None
    assert sf.share > 0.7 > 0.3 > weak.share >= 1 / 6 - 1e-9
    assert (sf.loadings > 0).all()  # sign rule: components sum >= 0
    assert sf.eigenvalue == pytest.approx(sf.share * len(sf.symbols))  # share = lambda_1 / trace, trace = N
    assert sf.n_agents == 200 and sf.symbols == tuple(NAMES) and sf.dropped == ()


def test_it_refuses_what_it_cannot_support_instead_of_guessing() -> None:
    x = _society()
    assert society_factor(x[:29], NAMES) is None  # fewer than 30 agents
    assert society_factor(x[:30], NAMES) is not None
    assert society_factor(np.where(np.arange(200)[:, None] == 3, np.nan, x), NAMES) is None
    assert society_factor(x[:, 0], NAMES) is None  # not 2-D
    one_varies = np.zeros((100, 6))
    one_varies[:, 2] = np.random.default_rng(1).standard_normal(100)
    assert society_factor(one_varies, NAMES) is None  # fewer than two symbols with any spread


def test_a_symbol_every_agent_scored_the_same_is_dropped_and_named() -> None:
    x = _society()
    x[:, 4] = 0.25
    sf = society_factor(x, NAMES)
    assert sf is not None and sf.dropped == ("SYM4",) and "SYM4" not in sf.symbols
    assert len(sf.loadings) == 5


def test_misuse_is_an_error_not_a_none() -> None:
    with pytest.raises(ValueError, match="min_agents"):
        society_factor(_society(), NAMES, min_agents=1)
    with pytest.raises(ValueError, match="symbols"):
        society_factor(_society(), NAMES[:3])


# --------------------------------------------------------------------------- boundary


def test_factors_depend_on_numpy_pandas_the_standard_library_and_the_analysis_siblings_only() -> None:
    tree = ast.parse(Path(factors.__file__).read_text(encoding="utf-8"))
    absolute: set[str] = set()
    relative: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            absolute |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            (relative if node.level else absolute).add((node.module or "").split(".")[0])
    assert absolute <= {"__future__", "math", "collections", "dataclasses", "numpy", "pandas"}, absolute
    assert relative <= {"rmt", "spectral"}, relative
