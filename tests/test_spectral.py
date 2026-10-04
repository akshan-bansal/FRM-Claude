"""Covariance filters, spectral decompositions and risk attribution.

References are independent where they can be: scikit-learn for Ledoit-Wolf, scipy for the Kupiec
test, and a literal port of the sp100 script for the clip-to-mean filter and eigenfactor risk.
"""
from __future__ import annotations

import ast
import inspect
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.analysis import spectral
from trading_live_claude.analysis.rmt import _sorted_eigh, fit_rmt
from trading_live_claude.analysis.spectral import (
    PINNED_MP,
    MPSpec,
    classify_spectrum,
    compare_mp_estimators,
    correlation_to_covariance,
    covariance_to_correlation,
    default_filters,
    eigendecompose,
    eigenfactor_risk,
    eigenvector_localization,
    ewma_covariance,
    ewma_filter,
    floor_correlation_eigenvalues,
    kupiec_pof,
    ledoit_wolf_covariance,
    relative_frobenius_distance,
    risk_contribution,
    sample_covariance,
    trailing,
    var_coverage,
    var_forecasts,
)


def _structured(seed: int = 0, t: int = 500, n: int = 19) -> np.ndarray:
    """A market mode, a five-asset sector and idiosyncratic noise."""
    rng = np.random.default_rng(seed)
    market = rng.standard_normal((t, 1))
    sector = np.zeros((t, n))
    sector[:, :5] = rng.standard_normal((t, 1))
    out: np.ndarray = 0.9 * market + 0.5 * sector + 0.5 * rng.standard_normal((t, n))
    return out


# --------------------------------------------------------------------------- the pinned estimator


def test_pinned_spec_is_the_repos_own_defaults() -> None:
    """If rmt's defaults ever move, the pin must be moved on purpose, not by drift."""
    params = inspect.signature(fit_rmt).parameters
    assert PINNED_MP.tw_sigmas == params["tw_sigmas"].default
    assert PINNED_MP.lower_cut == params["lower_cut"].default
    assert PINNED_MP.noise_shape == params["noise_shape"].default


def test_spec_is_validated_and_records_every_parameter() -> None:
    meta = PINNED_MP.as_meta()
    assert meta == {"mp_estimator": "analysis.rmt.denoise_correlation", "mp_tw_sigmas": 2.0,
                    "mp_lower_cut": 0.0, "mp_noise_shape": "constant"}
    for bad in ({"tw_sigmas": -1.0}, {"tw_sigmas": float("nan")}, {"lower_cut": -0.1}, {"noise_shape": "gaussian"}):
        with pytest.raises(ValueError):
            MPSpec(**bad)


def _sp100_reference(corr: np.ndarray, n_obs: int) -> tuple[np.ndarray, float, int]:
    """sp100_mp_portfolio_risk.py's estimate_noise_sigma2 + mp_bounds + mp_filter_correlation, literally."""
    vals, vecs = np.linalg.eigh(corr)
    order = np.argsort(vals)[::-1]
    vals, vecs = vals[order], vecs[:, order]
    q = corr.shape[0] / n_obs
    bulk = vals[vals <= (1.0 + np.sqrt(q)) ** 2]
    sigma2 = 1.0 if bulk.size == 0 else float(bulk.mean())
    lam_plus = sigma2 * (1.0 + np.sqrt(q)) ** 2
    noise = vals <= lam_plus
    noise_mean = vals[noise].mean() if noise.any() else 1.0
    filtered_vals = vals.copy()
    filtered_vals[noise] = noise_mean
    m = vecs @ np.diag(filtered_vals) @ vecs.T
    m = (m + m.T) / 2.0
    d = np.sqrt(np.maximum(np.diag(m), 1e-12))
    m = m / np.outer(d, d)
    np.fill_diagonal(m, 1.0)
    return m, sigma2, int((~noise).sum())


@pytest.mark.parametrize("seed", range(5))
def test_clip_to_mean_is_rmt_with_no_tracy_widom_margin(seed: int) -> None:
    """The scope's 'three implementations' include sp100's filter; it is the pinned function at tw=0."""
    x = _structured(seed)
    corr = np.corrcoef(x, rowvar=False)
    ref, sigma2, n_signal = _sp100_reference(corr, len(x))
    run = compare_mp_estimators(corr, len(x))["clip_to_mean"]
    assert run.clean_corr is not None
    np.testing.assert_allclose(run.clean_corr, ref, atol=1e-9)
    assert run.sigma2 == pytest.approx(sigma2)
    assert run.n_signal == n_signal


def _borderline() -> np.ndarray:
    """100 bars, 40 assets (q = 0.4): the aspect ratio at which the Tracy-Widom margin is wide enough
    to change the answer. Found by search over random worlds; 13 of 500 such trials disagreed."""
    rng = np.random.default_rng(14)
    t, n = 100, 40
    market = rng.standard_normal((t, 1))
    sector = np.zeros((t, n))
    sector[:, :6] = rng.standard_normal((t, 1))
    out: np.ndarray = 0.9 * market + 0.3 * sector + 0.5 * rng.standard_normal((t, n))
    return out


def test_where_the_margin_matters_the_treatments_disagree_and_clip_to_mean_still_matches_the_script() -> None:
    """This is the case where the open choice between estimators changes a result, not just a number."""
    x = _borderline()
    corr = np.corrcoef(x, rowvar=False)
    runs = compare_mp_estimators(corr, len(x))
    assert (runs["pinned"].n_signal, runs["clip_to_mean"].n_signal) == (1, 2)
    ref, sigma2, n_signal = _sp100_reference(corr, len(x))
    assert runs["clip_to_mean"].clean_corr is not None and runs["pinned"].clean_corr is not None
    np.testing.assert_allclose(runs["clip_to_mean"].clean_corr, ref, atol=1e-9)
    assert n_signal == 2 and runs["clip_to_mean"].sigma2 == pytest.approx(sigma2)
    assert relative_frobenius_distance(runs["pinned"].clean_corr, runs["clip_to_mean"].clean_corr) > 0.01


@pytest.mark.parametrize("seed", range(10))
def test_the_margin_only_ever_raises_the_bar(seed: int) -> None:
    runs = compare_mp_estimators(np.corrcoef(_structured(seed), rowvar=False), 500)
    assert runs["pinned"].sigma2 >= runs["clip_to_mean"].sigma2 - 1e-12
    assert runs["pinned"].threshold >= runs["clip_to_mean"].threshold - 1e-12
    assert runs["pinned"].n_signal <= runs["clip_to_mean"].n_signal


def test_bare_bounds_at_sigma2_one_miss_structure_a_dominant_market_hides() -> None:
    x = _structured(0)
    corr = np.corrcoef(x, rowvar=False)
    runs = compare_mp_estimators(corr, len(x))
    assert runs["pinned"].n_signal == 2 and runs["bare_bounds"].n_signal == 1
    assert runs["bare_bounds"].clean_corr is None and runs["bare_bounds"].spec is None  # a count, no matrix
    assert runs["pinned"].spec == PINNED_MP and runs["clip_to_mean"].spec == MPSpec(tw_sigmas=0.0)
    report = classify_spectrum(eigendecompose(corr)[0], len(x))
    assert report.n_structural_if_sigma2_is_1 == runs["bare_bounds"].n_signal


def test_relative_distance_is_zero_for_identical_matrices_and_positive_otherwise() -> None:
    corr = np.corrcoef(_structured(1), rowvar=False)
    assert relative_frobenius_distance(corr, corr) == 0.0
    assert relative_frobenius_distance(np.eye(len(corr)), corr) > 0.1


# --------------------------------------------------------------------------- covariance filters


def test_ledoit_wolf_matches_scikit_learn_exactly() -> None:
    covariance = pytest.importorskip("sklearn.covariance")
    for seed in range(5):
        rng = np.random.default_rng(seed)
        a = rng.standard_normal((60, 40)) @ rng.standard_normal((40, 40))
        cov, shrink = ledoit_wolf_covariance(a)
        ref = covariance.LedoitWolf().fit(a)
        assert shrink == pytest.approx(ref.shrinkage_, abs=1e-12)
        np.testing.assert_allclose(cov, ref.covariance_, atol=1e-10)


def test_ledoit_wolf_beats_the_sample_covariance_when_assets_are_many_relative_to_bars() -> None:
    n = 40
    truth = 0.5 ** np.abs(np.subtract.outer(np.arange(n), np.arange(n)))
    chol = np.linalg.cholesky(truth)
    lw_loss, sample_loss = [], []
    for seed in range(20):
        x = np.random.default_rng(seed).standard_normal((60, n)) @ chol.T
        lw_loss.append(np.linalg.norm(ledoit_wolf_covariance(x)[0] - truth))
        sample_loss.append(np.linalg.norm(sample_covariance(x) - truth))
    assert np.mean(lw_loss) < 0.8 * np.mean(sample_loss)


def test_ledoit_wolf_shrinkage_is_a_proportion_and_the_result_is_positive_definite() -> None:
    a = np.random.default_rng(3).standard_normal((30, 40))  # more assets than bars: sample cov is singular
    cov, shrink = ledoit_wolf_covariance(a)
    assert 0.0 <= shrink <= 1.0
    assert np.linalg.eigvalsh(cov).min() > 0


def test_ewma_matches_a_brute_force_weighted_sum() -> None:
    x = np.random.default_rng(4).standard_normal((80, 4)) * np.array([1.0, 2.0, 0.5, 3.0])
    lam = 0.94
    w = np.array([lam ** (len(x) - 1 - i) for i in range(len(x))])
    ref = sum(wi * np.outer(r, r) for wi, r in zip(w, x, strict=True)) / w.sum()
    np.testing.assert_allclose(ewma_covariance(x, lam), ref, atol=1e-12)
    mu = (w[:, None] * x).sum(axis=0) / w.sum()
    ref_c = sum(wi * np.outer(r - mu, r - mu) for wi, r in zip(w, x, strict=True)) / w.sum()
    np.testing.assert_allclose(ewma_covariance(x, lam, demean=True), ref_c, atol=1e-12)


def test_ewma_weighs_recent_rows_more() -> None:
    x = np.random.default_rng(5).standard_normal((100, 3))
    bump_last, bump_first = x.copy(), x.copy()
    bump_last[-1] += 5.0
    bump_first[0] += 5.0
    base = ewma_covariance(x, 0.94)[0, 0]
    assert ewma_covariance(bump_last, 0.94)[0, 0] - base > 100 * (ewma_covariance(bump_first, 0.94)[0, 0] - base)
    with pytest.raises(ValueError, match="lam"):
        ewma_covariance(x, 1.0)


def test_floor_leaves_a_healthy_matrix_alone_and_lifts_a_degenerate_one() -> None:
    healthy = np.asarray(np.corrcoef(np.random.default_rng(6).standard_normal((500, 5)), rowvar=False),
                         dtype=np.float64)
    np.testing.assert_allclose(floor_correlation_eigenvalues(healthy, 0.2), healthy, atol=1e-12)
    near_clones = np.array([[1.0, 0.99, 0.3], [0.99, 1.0, 0.3], [0.3, 0.3, 1.0]])
    floored = floor_correlation_eigenvalues(near_clones, 0.2)
    np.testing.assert_allclose(np.diag(floored), 1.0)
    np.testing.assert_allclose(floored, floored.T)
    assert np.linalg.eigvalsh(floored).min() > np.linalg.eigvalsh(near_clones).min() + 0.05
    with pytest.raises(ValueError, match="floor"):
        floor_correlation_eigenvalues(healthy, 1.5)


def test_correlation_covariance_round_trip_and_zero_variance_is_an_error() -> None:
    cov = np.asarray(np.cov(np.random.default_rng(7).standard_normal((200, 4)), rowvar=False), dtype=np.float64)
    corr, vol = covariance_to_correlation(cov)
    np.testing.assert_allclose(correlation_to_covariance(corr, vol), cov, atol=1e-12)
    flat = cov.copy()
    flat[2, :] = 0.0
    flat[:, 2] = 0.0
    with pytest.raises(ValueError, match="zero variance"):
        covariance_to_correlation(flat)


def test_every_default_filter_returns_a_symmetric_positive_definite_matrix() -> None:
    window = _structured(3, t=252, n=10)
    for name, f in default_filters().items():
        cov = f(window)
        assert cov.shape == (10, 10), name
        np.testing.assert_allclose(cov, cov.T, atol=1e-12, err_msg=name)
        assert np.linalg.eigvalsh(cov).min() > 0, name


def test_trailing_shows_the_filter_only_the_last_rows() -> None:
    x = np.random.default_rng(8).standard_normal((200, 3))
    np.testing.assert_array_equal(trailing(sample_covariance, 60)(x), sample_covariance(x[-60:]))
    with pytest.raises(ValueError):
        trailing(sample_covariance, 1)


def test_the_mp_filter_needs_more_bars_than_assets() -> None:
    with pytest.raises(ValueError):
        default_filters()["mp"](np.random.default_rng(9).standard_normal((20, 30)))


def test_every_eigenvector_is_signed_long_the_basket_whatever_lapack_returns() -> None:
    for seed in range(30):
        a = np.random.default_rng(seed).standard_normal((12, 12))
        _, vecs = eigendecompose(a @ a.T)
        assert (vecs.sum(axis=0) >= 0).all(), seed


def test_eigendecompose_follows_rmt_conventions() -> None:
    corr = np.corrcoef(_structured(2), rowvar=False)
    vals, vecs = eigendecompose(corr)
    ref_vals, ref_vecs = _sorted_eigh(corr)
    # Same order, same signs, same numbers. Not the same bits: this one symmetrises its input first.
    np.testing.assert_allclose(vals, ref_vals, atol=1e-12)
    np.testing.assert_allclose(vecs, ref_vecs, atol=1e-10)
    assert (vals[:-1] >= vals[1:]).all() and (vecs.sum(axis=0) >= 0).all()
    np.testing.assert_allclose(vecs @ np.diag(vals) @ vecs.T, corr, atol=1e-10)
    with pytest.raises(ValueError):
        eigendecompose(np.ones((2, 3)))


# --------------------------------------------------------------------------- VaR coverage


@pytest.mark.parametrize(("x", "n", "p"), [(0, 250, 0.05), (3, 250, 0.01), (12, 250, 0.05),
                                           (25, 250, 0.05), (250, 250, 0.05), (13, 260, 0.05)])
def test_kupiec_matches_scipy(x: int, n: int, p: float) -> None:
    stats = pytest.importorskip("scipy.stats")
    lr, pv = kupiec_pof(x, n, p)
    ref_lr = -2.0 * (stats.binom.logpmf(x, n, p) - stats.binom.logpmf(x, n, x / n))
    assert lr == pytest.approx(ref_lr, rel=1e-9, abs=1e-9)
    assert pv == pytest.approx(stats.chi2.sf(ref_lr, 1), rel=1e-7, abs=1e-12)


def test_kupiec_is_exactly_uninformative_at_the_nominal_rate_and_validates_input() -> None:
    lr, pv = kupiec_pof(13, 260, 0.05)
    assert lr == pytest.approx(0.0, abs=1e-9) and pv == pytest.approx(1.0)
    for args in ((5, 0, 0.05), (-1, 10, 0.05), (11, 10, 0.05), (1, 10, 0.0), (1, 10, 1.0)):
        with pytest.raises(ValueError):
            kupiec_pof(*args)


def _equal(n: int) -> np.ndarray:
    return np.ones(n) / n


def test_a_forecast_never_sees_the_row_it_forecasts_or_anything_later() -> None:
    x = np.random.default_rng(10).standard_normal((400, 5))
    base = var_forecasts(x, _equal(5), window=100, cov_filter=ewma_filter(0.94))
    tampered = x.copy()
    tampered[250:] *= 9.0
    after = var_forecasts(tampered, _equal(5), window=100, cov_filter=ewma_filter(0.94))
    np.testing.assert_array_equal(base.times, after.times)
    seen = base.times <= 250  # a forecast at t uses rows < t, all untouched up to t = 250
    np.testing.assert_array_equal(base.sigma[seen], after.sigma[seen])
    np.testing.assert_array_equal(base.realized[base.times < 250], after.realized[after.times < 250])
    assert not np.allclose(base.sigma[~seen], after.sigma[~seen])


def test_horizons_do_not_overlap_and_variance_scales_with_the_horizon() -> None:
    x = np.random.default_rng(11).standard_normal((300, 3)) * 0.01
    cov = np.diag([1.0, 2.0, 3.0]) * 1e-4
    w = np.array([0.5, 0.3, 0.2])
    fc = var_forecasts(x, w, window=60, cov_filter=lambda _w: cov, horizon=5)
    assert (np.diff(fc.times) == 5).all()
    np.testing.assert_allclose(fc.sigma, math.sqrt(5 * float(w @ cov @ w)))
    t0 = int(fc.times[0])
    assert fc.realized[0] == pytest.approx(float(x[t0 : t0 + 5].sum(axis=0) @ w))


def test_forecasts_reject_impossible_requests() -> None:
    x = np.random.default_rng(12).standard_normal((50, 3))
    with pytest.raises(ValueError, match="rows"):
        var_forecasts(x, _equal(3), window=60, cov_filter=sample_covariance)
    with pytest.raises(ValueError, match="weights"):
        var_forecasts(x, _equal(4), window=20, cov_filter=sample_covariance)
    with pytest.raises(ValueError, match="shape"):
        var_forecasts(x, _equal(3), window=20, cov_filter=lambda _w: np.eye(2))
    with pytest.raises(ValueError, match="NaN"):
        var_forecasts(np.where(np.arange(150)[:, None] == 3, np.nan, 1.0) * np.ones((150, 3)), _equal(3),
                      window=20, cov_filter=sample_covariance)


def test_long_and_short_lose_in_opposite_tails() -> None:
    """A -10% day breaches a long book and not a short one. Symmetric noise could never tell them apart."""
    x = np.zeros((300, 2))
    x[150] = -0.10
    cov = np.diag([1e-4, 1e-4])
    cells = var_coverage(x, np.array([0.5, 0.5]), window=60, filters={"const": lambda _w: cov})["const"]
    by = {(c.side, c.confidence): c for c in cells}
    for conf in (0.95, 0.99):
        assert by[("long", conf)].breaches == 1 and by[("short", conf)].breaches == 0
        assert by[("long", conf)].n == 240


def _iid_world() -> np.ndarray:
    rng = np.random.default_rng(1)
    a = rng.standard_normal((8, 8)) * 0.3 + np.eye(8)
    out: np.ndarray = (rng.standard_normal((3000, 8)) @ a.T) * 0.01
    return out


def test_when_the_covariance_never_changes_window_filters_cover_at_the_nominal_rate() -> None:
    cov = var_coverage(_iid_world(), _equal(8), window=252, filters=default_filters())
    for name in ("rolling", "ledoit_wolf"):
        for cell in cov[name]:
            assert cell.kupiec_p > 0.01, (name, cell)
            assert abs(cell.rate - cell.expected_rate) < 0.01
    assert all(c.n == 2748 for cells in cov.values() for c in cells)


def _regimes(seed: int = 5, length: int = 300) -> np.ndarray:
    r = np.random.default_rng(seed)
    scale = np.where((np.arange(3000) // length) % 2 == 0, 1.0, 3.0)[:, None]
    out: np.ndarray = (r.standard_normal((3000, 6)) @ (np.eye(6) * 0.7 + 0.3).T) * 0.01 * scale
    return out


def test_window_filters_lag_a_volatility_regime_and_ewma_does_not() -> None:
    cov = var_coverage(_regimes(), _equal(6), window=252, filters=default_filters())
    short99 = {name: cells[1] for name, cells in cov.items()}  # (short, 0.99)
    for name in ("rolling", "ledoit_wolf", "mp"):
        assert short99[name].kupiec_p < 1e-6, name
        assert short99[name].rate > 0.025, name
    for name in ("ewma94", "ewma94+floor"):
        assert short99[name].kupiec_p > 0.05, name
        assert short99[name].rate < 0.015, name
    assert short99["rolling"].rate > 2 * short99["ewma94"].rate


# --------------------------------------------------------------------------- spectrum


def test_pure_noise_has_no_structural_eigenvalues() -> None:
    x = np.random.default_rng(0).standard_normal((2000, 40))
    rep = classify_spectrum(eigendecompose(np.corrcoef(x, rowvar=False))[0], len(x))
    assert rep.n_structural == 0 and rep.signal_share == 0.0
    assert rep.n_structural + rep.n_bulk + rep.n_below == 40
    assert rep.effective_rank > 30  # a flat spectrum spreads across nearly every direction


def test_classification_agrees_with_rmts_own_signal_count_and_is_internally_consistent() -> None:
    for seed in range(5):
        x = _structured(seed)
        vals, _ = eigendecompose(np.corrcoef(x, rowvar=False))
        rep = classify_spectrum(vals, len(x))
        assert rep.n_structural == fit_rmt(pd.DataFrame(x)).n_signal == 2
        assert rep.n_structural + rep.n_bulk + rep.n_below == rep.n_assets
        assert rep.structural_fraction + rep.bulk_fraction + rep.below_fraction == pytest.approx(1.0)
        assert 1.0 <= rep.effective_rank <= rep.n_assets
        assert 0.0 < rep.signal_share < 1.0 and rep.largest_to_second > 1.0
        assert rep.sigma2 < 1.0 and rep.lower_edge <= rep.lambda_minus < rep.lambda_plus <= rep.threshold


def test_classification_rejects_more_assets_than_bars_and_degenerate_input() -> None:
    with pytest.raises(ValueError):
        classify_spectrum(np.ones(30), 20)
    with pytest.raises(ValueError, match="at least two"):
        classify_spectrum(np.array([1.0]), 100)


def test_localization_separates_a_spread_vector_from_a_single_asset_vector() -> None:
    n = 10
    uniform = np.ones(n) / math.sqrt(n)
    onehot = np.zeros(n)
    onehot[3] = 1.0
    rep = eigenvector_localization(np.array([3.0, 1.0]), np.column_stack([uniform, onehot]), top_k=2)
    assert rep.inverse_participation_ratio[0] == pytest.approx(1 / n)
    assert rep.participation_ratio[0] == pytest.approx(n)
    assert rep.entropy[0] == pytest.approx(math.log(n))
    assert rep.inverse_participation_ratio[1] == pytest.approx(1.0)
    assert rep.participation_ratio[1] == pytest.approx(1.0) and rep.entropy[1] == pytest.approx(0.0)
    assert rep.max_abs_loading[1] == 1.0 and rep.factor.tolist() == [1, 2]


# --------------------------------------------------------------------------- risk attribution


def test_risk_contributions_add_up_and_match_a_numerical_derivative() -> None:
    cov = np.cov(_structured(4, t=400, n=6), rowvar=False)
    w = np.array([0.3, 0.1, -0.05, 0.25, 0.2, 0.2])
    rc = risk_contribution(w, cov)
    assert rc.component.sum() == pytest.approx(rc.volatility, rel=1e-12)
    assert rc.share.sum() == pytest.approx(1.0)
    h = 1e-6
    for i in range(6):
        up, dn = w.copy(), w.copy()
        up[i] += h
        dn[i] -= h
        numeric = (math.sqrt(up @ cov @ up) - math.sqrt(dn @ cov @ dn)) / (2 * h)
        assert rc.marginal[i] == pytest.approx(numeric, rel=1e-6)


def test_risk_contribution_refuses_a_portfolio_with_no_variance() -> None:
    with pytest.raises(ValueError, match="variance"):
        risk_contribution(np.zeros(3), np.eye(3))


def _sp100_factor_risk(eigenvalues: np.ndarray, eigenvectors: np.ndarray, weights: np.ndarray,
                       annual_vol: np.ndarray) -> np.ndarray:
    """sp100_mp_portfolio_risk.py's factor_risk, literally: variance contribution per eigenfactor."""
    exposure = eigenvectors.T @ (annual_vol * weights)
    out: np.ndarray = eigenvalues * exposure**2
    return out


def test_eigenfactor_risk_reconciles_and_matches_the_scripts_decomposition() -> None:
    x = _structured(6, t=600, n=12)
    cov = np.cov(x, rowvar=False)
    corr, vol = covariance_to_correlation(cov)
    clean = compare_mp_estimators(corr, len(x))["pinned"].clean_corr
    assert clean is not None
    w = np.linspace(0.02, 0.15, 12)
    w = w / w.sum()
    res = eigenfactor_risk(clean, vol, w)
    full = correlation_to_covariance(clean, vol)
    assert res.total == pytest.approx(float(w @ full @ w), rel=1e-10)  # the SAME number the volatility reports
    assert res.share.sum() == pytest.approx(1.0) and abs(res.residual) < 1e-12
    vals, vecs = np.linalg.eigh(clean)
    order = np.argsort(vals)[::-1]
    np.testing.assert_allclose(res.variance, _sp100_factor_risk(vals[order], vecs[:, order], w, vol), atol=1e-12)
    assert res.variance[0] > res.variance[-1]  # the market mode dominates an all-long book


def test_eigenfactor_risk_refuses_numbers_that_do_not_reconcile() -> None:
    not_a_correlation = np.array([[1.0, 2.0], [2.0, 1.0]])  # eigenvalues 3 and -1
    with pytest.raises(ValueError, match="reconcile"):
        eigenfactor_risk(not_a_correlation, np.ones(2), np.array([1.0, -1.0]))
    with pytest.raises(ValueError, match="vol"):
        eigenfactor_risk(np.eye(2), np.array([1.0, 0.0]), np.ones(2))


# --------------------------------------------------------------------------- boundary


def test_spectral_depends_on_numpy_the_standard_library_and_rmt_only() -> None:
    tree = ast.parse(Path(spectral.__file__).read_text(encoding="utf-8"))
    absolute: set[str] = set()
    relative: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            absolute |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            (relative if node.level else absolute).add((node.module or "").split(".")[0])
    assert absolute <= {"__future__", "math", "statistics", "collections", "dataclasses", "numpy"}, absolute
    assert relative == {"rmt"}, relative
