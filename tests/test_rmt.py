"""Marchenko-Pastur denoising: band correctness on pure noise, planted-factor recovery, output
invariants, and the no-lookahead contract of the frozen projection."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.analysis.rmt import (
    NOISE_SHAPES,
    denoise_correlation,
    fit_rmt,
    gaussian_nll,
    marchenko_pastur_bounds,
    marchenko_pastur_pdf,
    min_variance_weights,
    shape_quantiles,
    volume_weights,
)


def _frame(x: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame(x, columns=[f"A{i}" for i in range(x.shape[1])])


def test_mp_pdf_integrates_to_one() -> None:
    q = 0.25
    lo, hi = marchenko_pastur_bounds(q)
    xs = np.linspace(lo, hi, 20001)
    assert np.trapezoid(marchenko_pastur_pdf(xs, q), xs) == pytest.approx(1.0, abs=5e-3)


def test_bounds_reject_bad_q() -> None:
    with pytest.raises(ValueError):
        marchenko_pastur_bounds(1.5)


def test_pure_noise_has_no_signal() -> None:
    rng = np.random.default_rng(0)
    r = fit_rmt(_frame(rng.standard_normal((2000, 40))))
    assert r.n_signal == 0
    assert r.eigenvalues.max() < r.lambda_plus * 1.05
    # nothing to keep -> denoised matrix collapses to identity
    np.testing.assert_allclose(r.clean_corr, np.eye(40), atol=1e-8)


def test_planted_market_factor_is_recovered() -> None:
    rng = np.random.default_rng(1)
    t, n = 1000, 20
    market = rng.standard_normal((t, 1))
    x = 0.8 * market + 0.6 * rng.standard_normal((t, n))
    r = fit_rmt(_frame(x))
    assert r.n_signal == 1
    # market-mode eigenvector ~ uniform, signed long
    v = r.eigenvectors[:, 0]
    assert (v > 0).all()
    np.testing.assert_allclose(v, np.full(n, 1 / np.sqrt(n)), atol=0.05)
    # sigma2 is corrected down from 1 because the market mode took its share of variance
    assert r.sigma2 < 0.6


@pytest.mark.parametrize("seed", range(10))
def test_strong_market_mode_does_not_cascade(seed: int) -> None:
    """Regression: a fixed-point sigma2 iteration let a dominant market mode drag lambda_+ down
    until most of the bulk read as signal (1 -> 10 of 19 modes on the real crypto basket)."""
    rng = np.random.default_rng(seed)
    t, n = 500, 19
    market = rng.standard_normal((t, 1))
    sector = np.zeros((t, n))
    sector[:, :5] = rng.standard_normal((t, 1))
    x = 0.9 * market + 0.5 * sector + 0.5 * rng.standard_normal((t, n))
    assert fit_rmt(_frame(x)).n_signal == 2


def test_denoised_matrix_invariants() -> None:
    rng = np.random.default_rng(2)
    f = rng.standard_normal((400, 2))
    x = f @ rng.standard_normal((2, 15)) + rng.standard_normal((400, 15))
    r = fit_rmt(_frame(x))
    c = r.clean_corr
    np.testing.assert_allclose(c, c.T, atol=1e-12)
    np.testing.assert_allclose(np.diag(c), 1.0)
    assert np.linalg.eigvalsh(c).min() > 0
    assert r.embed(6).shape == (15, 6)


def test_needs_more_bars_than_assets() -> None:
    with pytest.raises(ValueError, match="more bars than assets"):
        fit_rmt(_frame(np.random.default_rng(3).standard_normal((10, 12))))


def test_factor_returns_are_row_local_no_lookahead() -> None:
    """Projection through a train-fitted RMT must not let future rows affect earlier ones."""
    rng = np.random.default_rng(4)
    x = 0.7 * rng.standard_normal((600, 1)) + rng.standard_normal((600, 10))
    df = _frame(x)
    r = fit_rmt(df.iloc[:400])
    base = r.factor_returns(df)
    tampered = df.copy()
    tampered.iloc[500:] *= 50.0
    after = r.factor_returns(tampered)
    pd.testing.assert_frame_equal(base.iloc[:500], after.iloc[:500])
    assert not np.allclose(base.iloc[500:], after.iloc[500:])


def _structured(seed: int = 5, t: int = 600, n: int = 12) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    x = 0.8 * rng.standard_normal((t, 1)) + rng.standard_normal((t, n))
    x[:, 1] = x[:, 0] + 0.1 * rng.standard_normal(t)   # near-clone -> tiny eigenvalue
    return _frame(x)


@pytest.mark.parametrize("shape", NOISE_SHAPES)
def test_noise_shapes_preserve_trace_vectors_and_psd(shape: str) -> None:
    r = fit_rmt(_structured(), noise_shape=shape)  # type: ignore[arg-type]
    np.testing.assert_allclose(r.cleaned_eigenvalues.sum(), r.eigenvalues.sum(), rtol=1e-10)
    np.testing.assert_allclose(r.cleaned_eigenvalues[: r.n_signal], r.eigenvalues[: r.n_signal])
    c = r.clean_corr
    np.testing.assert_allclose(np.diag(c), 1.0)
    assert np.linalg.eigvalsh(c).min() > 0
    assert (np.diff(r.cleaned_eigenvalues) <= 1e-12).all()   # rank order kept


def test_shape_quantiles_have_expected_edge_weight() -> None:
    lo, hi = 0.2, 1.8
    spread = {s: np.std(shape_quantiles(s, 200, lo, hi, 0.05, 1.0)) for s in NOISE_SHAPES}
    assert spread["constant"] == 0.0
    assert spread["uniform"] < spread["arcsine"] < spread["binomial"]
    arc = shape_quantiles("arcsine", 200, lo, hi, 0.05, 1.0)
    assert lo < arc.min() and arc.max() < hi


def test_lower_cut_keeps_low_end_eigenvalues() -> None:
    df = _structured()
    base = fit_rmt(df)
    assert base.n_lower_kept == 0
    cut = float(base.eigenvalues[-1]) + 1e-6
    r = fit_rmt(df, lower_cut=cut)
    assert r.n_lower_kept == 1
    assert r.cleaned_eigenvalues[-1] == pytest.approx(r.eigenvalues[-1])
    assert base.cleaned_eigenvalues[-1] > base.eigenvalues[-1]   # replaced when not kept
    assert 0.0 <= r.lower_edge() < r.lambda_minus


def test_uniform_volume_weights_leave_market_mode_unchanged() -> None:
    df = _structured()
    plain = fit_rmt(df)
    same = fit_rmt(df, eig1_weights=np.ones(df.shape[1]))
    np.testing.assert_allclose(plain.embed(6), same.embed(6), atol=1e-12)


def test_volume_weights_tilt_eig1_and_keep_unit_factor_variance() -> None:
    df = _structured()
    dv = pd.DataFrame(np.tile(np.linspace(1, 50, df.shape[1]), (10, 1)), columns=df.columns)
    w = volume_weights(dv)
    assert w.mean() == pytest.approx(1.0)
    r = fit_rmt(df, eig1_weights=w)
    u = r.directions(6)
    assert np.linalg.norm(u[:, 0]) == pytest.approx(1.0)
    assert abs(u[-1, 0]) > abs(u[2, 0])                      # heavier-volume asset loads more
    f = r.factor_returns(df, k=6)
    np.testing.assert_allclose(f.var(ddof=0).to_numpy(), 1.0, rtol=1e-8)
    assert r.embed(6).shape == (df.shape[1], 6)


def test_oos_scores_prefer_truth_over_identity() -> None:
    rng = np.random.default_rng(6)
    true = np.full((8, 8), 0.6) + 0.4 * np.eye(8)
    z = rng.multivariate_normal(np.zeros(8), true, size=4000)
    assert gaussian_nll(true, z).mean() < gaussian_nll(np.eye(8), z).mean()
    w = min_variance_weights(true, np.ones(8))
    assert w.sum() == pytest.approx(1.0)
    assert np.var(z @ w) < np.var(z.mean(axis=1)) + 1e-3


def test_denoise_default_matches_constant_residual() -> None:
    rng = np.random.default_rng(7)
    x = 0.8 * rng.standard_normal((500, 1)) + rng.standard_normal((500, 10))
    z = (x - x.mean(0)) / x.std(0)
    d = denoise_correlation(z.T @ z / 500, 500)
    noise = d.cleaned_eigenvalues[1:]
    np.testing.assert_allclose(noise, noise.mean())
