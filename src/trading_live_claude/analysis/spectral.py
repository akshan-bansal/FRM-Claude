"""Covariance filters, spectral decompositions and risk attribution: the building blocks of L2.

Teleported, not imported: the algorithms below were re-implemented here from three research scripts
named in ``HOMOGENEOUS_GRAPH_SCOPE.md`` section 3, so nothing in the engine depends on those files.

    financial_spectral_filters.py  Ledoit-Wolf covariance, spectral decomposition, spectrum
                                   classification, eigenvector localization
    sp100_mp_portfolio_risk.py     eigendecomposition, the clip-to-mean Marchenko-Pastur filter,
                                   risk contribution, eigenfactor risk
    bsm_spectral.py (part 2)       covariance filters (rolling, EWMA, eigenvalue floor) compared by
                                   VaR coverage with a Kupiec test

Left behind on purpose: the 50-distribution fitting registry (a research harness), the global
warning suppression, the Wikipedia and Yahoo downloads, and the ``FinancialSpectralEngine`` class
(its ``eigenvectors`` attribute shadows its own ``eigenvectors()`` method, so that method could
never be called).

One Marchenko-Pastur estimator is pinned, as the scope requires: ``rmt.denoise_correlation`` with the
parameters in :data:`PINNED_MP`. The three implementations the scope found are not three algorithms.
The sp100 clip-to-mean filter is that same function with ``tw_sigmas=0``, and the bare bounds are
the same bounds at ``sigma2 = 1``. :func:`compare_mp_estimators` runs all three side by side, labelled
as comparisons, so the difference is measured and never silently chosen.

Contract, same as the rest of ``analysis``: a ruler. Pure numpy, no I/O, never imported by execution,
risk, brokers, strategies, monitor or the daemon. Lookahead is the caller's concern for the fit
window; the one function here that walks through time, :func:`var_forecasts`, only ever shows a
filter rows strictly before the row it is forecasting, and a test tampers with the future to prove it.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .rmt import (
    NOISE_SHAPES,
    Denoised,
    NoiseShape,
    denoise_correlation,
    estimate_noise_sigma2,
    marchenko_pastur_bounds,
    signal_threshold,
    tracy_widom_scale_lower,
)

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]
CovFilter = Callable[[FloatArray], FloatArray]

MP_ESTIMATOR = "analysis.rmt.denoise_correlation"
DEFAULT_EIGENVALUE_FLOOR = 0.2  # the 2-asset analogue of bsm_spectral's rho cap of 0.8 (1 - 0.8)


# --------------------------------------------------------------------------- the pinned estimator


@dataclass(frozen=True)
class MPSpec:
    """Parameters of the one Marchenko-Pastur estimator in use. Written onto every dependent edge."""

    tw_sigmas: float = 2.0
    lower_cut: float = 0.0
    noise_shape: NoiseShape = "constant"

    def __post_init__(self) -> None:
        if not (math.isfinite(self.tw_sigmas) and self.tw_sigmas >= 0.0):
            raise ValueError(f"tw_sigmas must be a finite number >= 0, got {self.tw_sigmas!r}")
        if not (math.isfinite(self.lower_cut) and self.lower_cut >= 0.0):
            raise ValueError(f"lower_cut must be a finite number >= 0, got {self.lower_cut!r}")
        if self.noise_shape not in NOISE_SHAPES:
            raise ValueError(f"noise_shape must be one of {NOISE_SHAPES}, got {self.noise_shape!r}")

    def as_meta(self) -> dict[str, float | str]:
        return {"mp_estimator": MP_ESTIMATOR, "mp_tw_sigmas": self.tw_sigmas,
                "mp_lower_cut": self.lower_cut, "mp_noise_shape": self.noise_shape}


# Provisional pin: the repo's own estimator at its own defaults (``fit_rmt``'s). The scope leaves
# the final choice to the user; flipping it is this one line, and every edge records what was used.
PINNED_MP = MPSpec()


def denoise(corr: FloatArray, n_obs: int, spec: MPSpec = PINNED_MP) -> Denoised:
    """The single call site of the pinned estimator in this module."""
    return denoise_correlation(corr, n_obs, tw_sigmas=spec.tw_sigmas, lower_cut=spec.lower_cut,
                               noise_shape=spec.noise_shape)


# --------------------------------------------------------------------------- inputs and helpers


def _as_returns(x: object, name: str = "returns") -> FloatArray:
    a = np.asarray(x, dtype=float)
    if a.ndim != 2:
        raise ValueError(f"{name} must be 2-D (observations x assets); got shape {a.shape}")
    if not np.isfinite(a).all():
        raise ValueError(f"{name} contain NaN/inf")
    return a


def _check_weights(weights: object, n: int) -> FloatArray:
    w = np.asarray(weights, dtype=float)
    if w.shape != (n,) or not np.isfinite(w).all():
        raise ValueError(f"weights must be {n} finite numbers; got shape {w.shape}")
    return w


def _symmetrize(m: FloatArray) -> FloatArray:
    out: FloatArray = (m + m.T) / 2.0
    return out


def covariance_to_correlation(cov: FloatArray) -> tuple[FloatArray, FloatArray]:
    """``(correlation, volatility)``. A zero-variance asset has no correlation: that is an error."""
    vol = np.sqrt(np.diag(cov))
    if not (vol > 0).all():
        raise ValueError("an asset has zero variance; its correlation is undefined")
    corr = cov / np.outer(vol, vol)
    np.fill_diagonal(corr, 1.0)
    return _symmetrize(corr), vol


def correlation_to_covariance(corr: FloatArray, vol: FloatArray) -> FloatArray:
    """``D C D`` for volatilities ``vol`` (in whatever period the caller wants the covariance)."""
    out: FloatArray = corr * np.outer(vol, vol)
    return out


def eigendecompose(matrix: object) -> tuple[FloatArray, FloatArray]:
    """Eigenvalues descending with their eigenvectors as columns.

    Each eigenvector is signed so its components sum to >= 0, the same rule as ``rmt``, which makes
    the market mode read "long the basket" and keeps the output identical across platforms.
    """
    m = np.asarray(matrix, dtype=float)
    if m.ndim != 2 or m.shape[0] != m.shape[1] or not np.isfinite(m).all():
        raise ValueError(f"expected a finite square matrix; got shape {m.shape}")
    vals, vecs = np.linalg.eigh(_symmetrize(m))
    order = np.argsort(vals)[::-1]
    vals, vecs = vals[order], vecs[:, order]
    signs = np.where(vecs.sum(axis=0) < 0.0, -1.0, 1.0)
    return vals, vecs * signs


# --------------------------------------------------------------------------- covariance filters
# Each takes an estimation window (rows = bars, columns = assets) and returns an N x N covariance
# in the window's own period. The caller guarantees the window precedes whatever it is used to price.


def sample_covariance(x: object, *, ddof: int = 1) -> FloatArray:
    a = _as_returns(x)
    if len(a) <= ddof:
        raise ValueError(f"need more than {ddof} rows for a sample covariance; got {len(a)}")
    return np.atleast_2d(np.cov(a, rowvar=False, ddof=ddof))


def ewma_covariance(x: object, lam: float, *, demean: bool = False) -> FloatArray:
    """RiskMetrics-style exponentially weighted covariance, weights normalised over the window.

    The newest row weighs most. Zero-mean by default, as RiskMetrics and the original script have
    it; ``demean=True`` removes the weighted mean first (no small-sample correction either way).
    """
    a = _as_returns(x)
    if not 0.0 < lam < 1.0:
        raise ValueError(f"lam must lie strictly between 0 and 1, got {lam!r}")
    w = lam ** np.arange(len(a) - 1, -1, -1)
    w = w / w.sum()
    if demean:
        a = a - w @ a
    return _symmetrize((a * w[:, None]).T @ a)


def ledoit_wolf_covariance(x: object) -> tuple[FloatArray, float]:
    """Ledoit-Wolf (2004) shrinkage toward a scaled identity. Returns ``(covariance, shrinkage)``.

    Re-derived from the paper: with ``S`` the 1/n sample covariance, ``m = tr(S)/p``,
    ``d2 = |S - mI|^2 / p`` and ``b2 = sum_k |x_k x_k' - S|^2 / (n^2 p)`` capped at ``d2``, the
    estimator is ``(1 - b2/d2) S + (b2/d2) m I``. Uses the sum identity
    ``sum_k |x_k x_k' - S|^2 = sum_k |x_k|^4 - n |S|^2``.
    """
    a = _as_returns(x)
    n, p = a.shape
    if n < 2:
        raise ValueError("need at least two rows")
    xc = a - a.mean(axis=0)
    s = xc.T @ xc / n
    mu = float(np.trace(s) / p)
    s_fro2 = float((s * s).sum())
    d2 = (s_fro2 - p * mu * mu) / p
    sum_norm4 = float((((xc * xc).sum(axis=1)) ** 2).sum())
    b2 = min((sum_norm4 / n - s_fro2) / (n * p), d2)
    shrink = 0.0 if (b2 <= 0.0 or d2 <= 0.0) else b2 / d2
    cov = (1.0 - shrink) * s + shrink * mu * np.eye(p)
    return _symmetrize(cov), float(shrink)


def floor_correlation_eigenvalues(corr: FloatArray, floor: float = DEFAULT_EIGENVALUE_FLOOR) -> FloatArray:
    """Raise every eigenvalue below ``floor`` to ``floor``, rebuild, and rescale to a unit diagonal.

    The floor is exact before the rescale and approximate after it. A matrix whose eigenvalues
    already clear the floor comes back unchanged. The N-asset generalisation of bsm_spectral's
    correlation cap, which is not numerically identical to clipping a single rho.
    """
    if not 0.0 <= floor <= 1.0:
        raise ValueError(f"floor must lie in [0, 1], got {floor!r}")
    vals, vecs = eigendecompose(corr)
    rebuilt = (vecs * np.maximum(vals, floor)) @ vecs.T
    d = np.sqrt(np.diag(rebuilt))
    out = rebuilt / np.outer(d, d)
    np.fill_diagonal(out, 1.0)
    return _symmetrize(out)


def trailing(f: CovFilter, n: int) -> CovFilter:
    """``f`` applied to only the last ``n`` rows of whatever window it is given."""
    if n < 2:
        raise ValueError("a trailing window needs at least two rows")

    def _f(x: FloatArray) -> FloatArray:
        return f(x[-n:])

    return _f


def ewma_filter(lam: float) -> CovFilter:
    return lambda x: ewma_covariance(x, lam)


def ewma_floor_filter(lam: float, floor: float = DEFAULT_EIGENVALUE_FLOOR) -> CovFilter:
    """EWMA volatilities with an eigenvalue-floored correlation matrix."""

    def _f(x: FloatArray) -> FloatArray:
        corr, vol = covariance_to_correlation(ewma_covariance(x, lam))
        return correlation_to_covariance(floor_correlation_eigenvalues(corr, floor), vol)

    return _f


def mp_filter(spec: MPSpec = PINNED_MP) -> CovFilter:
    """Sample volatilities with the pinned Marchenko-Pastur-denoised correlation (needs rows > assets)."""

    def _f(x: FloatArray) -> FloatArray:
        cov = sample_covariance(x)
        corr, vol = covariance_to_correlation(cov)
        return correlation_to_covariance(denoise(corr, len(x), spec).clean_corr, vol)

    return _f


def default_filters() -> dict[str, CovFilter]:
    """The comparison set. RiskMetrics' 0.94 and 0.97 and the 0.2 floor are the original's choices."""
    return {
        "rolling": lambda x: sample_covariance(x),
        "ewma94": ewma_filter(0.94),
        "ewma97": ewma_filter(0.97),
        "ewma94+floor": ewma_floor_filter(0.94),
        "ledoit_wolf": lambda x: ledoit_wolf_covariance(x)[0],
        "mp": mp_filter(),
    }


# --------------------------------------------------------------------------- VaR coverage


def kupiec_pof(breaches: int, n: int, expected_rate: float) -> tuple[float, float]:
    """Kupiec (1995) proportion-of-failures test: ``(likelihood ratio, chi-square(1) p-value)``.

    Tests unconditional coverage only. It says nothing about breaches clustering in time, has little
    power at small ``n``, and a pass is the absence of evidence against the filter, not evidence for it.
    """
    if n <= 0 or not 0 <= breaches <= n:
        raise ValueError(f"need 0 <= breaches <= n and n > 0; got breaches={breaches}, n={n}")
    if not 0.0 < expected_rate < 1.0:
        raise ValueError(f"expected_rate must lie strictly between 0 and 1, got {expected_rate!r}")

    def loglik(rate: float) -> float:
        out = 0.0
        if breaches > 0:
            out += breaches * math.log(rate)
        if n - breaches > 0:
            out += (n - breaches) * math.log(1.0 - rate)
        return out

    lr = max(0.0, -2.0 * (loglik(expected_rate) - loglik(breaches / n)))
    return lr, math.erfc(math.sqrt(lr / 2.0))  # chi-square(1) survival function


@dataclass(frozen=True)
class VarForecasts:
    """One forecast per non-overlapping horizon: the filter saw only rows strictly before ``times[k]``."""

    times: IntArray  # first row of each realised horizon
    sigma: FloatArray  # forecast standard deviation of the portfolio return over the horizon
    realized: FloatArray  # realised portfolio return over rows [t, t + horizon)


def var_forecasts(returns: object, weights: object, *, window: int, cov_filter: CovFilter,
                  horizon: int = 1) -> VarForecasts:
    """Walk forward: filter the ``window`` rows before ``t``, forecast the horizon starting at ``t``.

    Horizons do not overlap (``t`` advances by ``horizon``), which the Kupiec test needs. Returns are
    treated as additive over the horizon (exact for log returns) and the covariance scales with it.
    """
    x = _as_returns(returns)
    t_total, n = x.shape
    w = _check_weights(weights, n)
    if horizon < 1 or window < 2:
        raise ValueError("horizon must be >= 1 and window >= 2")
    if t_total < window + horizon:
        raise ValueError(f"need at least window + horizon = {window + horizon} rows; got {t_total}")
    times = np.arange(window, t_total - horizon + 1, horizon, dtype=np.int64)
    sigma = np.empty(len(times))
    realized = np.empty(len(times))
    for k, t in enumerate(times):
        cov = np.asarray(cov_filter(x[t - window : t]), dtype=float)
        if cov.shape != (n, n):
            raise ValueError(f"filter returned shape {cov.shape}, expected {(n, n)}")
        sigma[k] = math.sqrt(max(float(w @ cov @ w) * horizon, 0.0))
        realized[k] = float(x[t : t + horizon].sum(axis=0) @ w)
    return VarForecasts(times, sigma, realized)


@dataclass(frozen=True)
class CoverageCell:
    side: str  # "long" loses when the return falls below -VaR, "short" when it rises above +VaR
    confidence: float
    n: int
    breaches: int
    rate: float
    expected_rate: float
    kupiec_lr: float
    kupiec_p: float


def var_coverage(returns: object, weights: object, *, window: int, filters: Mapping[str, CovFilter],
                 confidences: tuple[float, ...] = (0.95, 0.99),
                 horizon: int = 1) -> dict[str, tuple[CoverageCell, ...]]:
    """Gaussian VaR from each filter's covariance, scored by breach rate and Kupiec p-value.

    The comparison between filters on the same data is the point. A Gaussian VaR will breach more
    often than nominal at 99% whatever filter feeds it, because returns have fat tails.
    """
    out: dict[str, tuple[CoverageCell, ...]] = {}
    for name, f in filters.items():
        fc = var_forecasts(returns, weights, window=window, cov_filter=f, horizon=horizon)
        cells: list[CoverageCell] = []
        for side in ("short", "long"):
            for c in confidences:
                if not 0.5 < c < 1.0:
                    raise ValueError(f"confidence must lie in (0.5, 1), got {c!r}")
                var = statistics.NormalDist().inv_cdf(c) * fc.sigma
                hit = fc.realized > var if side == "short" else fc.realized < -var
                n, x = len(hit), int(hit.sum())
                lr, p = kupiec_pof(x, n, 1.0 - c)
                cells.append(CoverageCell(side, c, n, x, x / n, 1.0 - c, lr, p))
        out[name] = tuple(cells)
    return out


# --------------------------------------------------------------------------- spectrum


@dataclass(frozen=True)
class SpectrumReport:
    """Numbers only. No regime label: any label's thresholds would be a hidden choice."""

    n_assets: int
    n_obs: int
    q: float
    sigma2: float
    lambda_minus: float
    lambda_plus: float
    threshold: float  # lambda_plus plus the Tracy-Widom margin: above is structure
    lower_edge: float  # MP-consistent lower boundary: below is near-collinearity
    n_structural: int
    n_bulk: int
    n_below: int
    structural_fraction: float
    bulk_fraction: float
    below_fraction: float
    signal_share: float  # share of total variance in the structural eigenvalues
    largest: float
    largest_to_second: float
    effective_rank: float  # exp(entropy of the normalised spectrum)
    n_structural_if_sigma2_is_1: int  # what the unscaled bounds would have found


def classify_spectrum(eigenvalues: object, n_obs: int, *, mp: MPSpec = PINNED_MP) -> SpectrumReport:
    """Count structure, bulk and near-collinear directions with the pinned estimator's bounds.

    ``eigenvalues`` come from a correlation matrix estimated from ``n_obs`` bars (trace = N). The
    bounds use the estimated noise variance, not 1: a market mode that holds a third of the
    variance leaves the bulk well under 1, and sigma2 = 1 puts the upper edge too high and files real
    sector structure under noise (``n_structural_if_sigma2_is_1`` shows by how much).
    """
    lam = np.sort(np.asarray(eigenvalues, dtype=float))[::-1]
    if lam.ndim != 1 or len(lam) < 2 or not np.isfinite(lam).all():
        raise ValueError("eigenvalues must be a finite 1-D array of at least two values")
    n = len(lam)
    sigma2 = estimate_noise_sigma2(lam, n_obs, tw_sigmas=mp.tw_sigmas)
    lo, hi = marchenko_pastur_bounds(n / n_obs, sigma2)
    thr = signal_threshold(n, n_obs, sigma2, mp.tw_sigmas)
    lower = max(lo - mp.tw_sigmas * tracy_widom_scale_lower(n, n_obs, sigma2), 0.0)
    structural, below = lam > thr, lam < lower
    bulk = ~structural & ~below
    pos = np.maximum(lam, 0.0)
    total = float(pos.sum())
    p = pos / total if total > 0 else pos
    entropy = float(-(p[p > 0] * np.log(p[p > 0])).sum())
    return SpectrumReport(
        n_assets=n, n_obs=n_obs, q=n / n_obs, sigma2=sigma2, lambda_minus=lo, lambda_plus=hi,
        threshold=thr, lower_edge=lower, n_structural=int(structural.sum()), n_bulk=int(bulk.sum()),
        n_below=int(below.sum()), structural_fraction=float(structural.mean()),
        bulk_fraction=float(bulk.mean()), below_fraction=float(below.mean()),
        signal_share=float(pos[structural].sum() / total) if total > 0 else 0.0,
        largest=float(lam[0]), largest_to_second=float(lam[0] / lam[1]) if lam[1] > 0 else math.inf,
        effective_rank=math.exp(entropy),
        n_structural_if_sigma2_is_1=int((lam > (1.0 + math.sqrt(n / n_obs)) ** 2).sum()),
    )


@dataclass(frozen=True)
class LocalizationReport:
    """Per eigenvector: how concentrated it is on a few assets. ``|v|^2`` is normalised first."""

    factor: IntArray  # 1-based
    eigenvalue: FloatArray
    explained_share: FloatArray
    max_abs_loading: FloatArray
    l1_norm: FloatArray
    inverse_participation_ratio: FloatArray  # 1/N for a uniform vector, 1 for a single-asset vector
    participation_ratio: FloatArray  # 1 / IPR: roughly how many assets the vector spans
    entropy: FloatArray


def eigenvector_localization(eigenvalues: object, eigenvectors: object, top_k: int = 10) -> LocalizationReport:
    lam = np.asarray(eigenvalues, dtype=float)
    v = np.asarray(eigenvectors, dtype=float)
    if v.ndim != 2 or lam.shape != (v.shape[1],):
        raise ValueError("eigenvectors must be N x N with one eigenvalue per column")
    k = min(top_k, v.shape[1])
    cols = v[:, :k]
    p = cols**2 / (cols**2).sum(axis=0)
    ipr = (p**2).sum(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        entropy = -np.where(p > 0, p * np.log(p), 0.0).sum(axis=0)
    total = float(np.maximum(lam, 0.0).sum())
    return LocalizationReport(
        factor=np.arange(1, k + 1, dtype=np.int64), eigenvalue=lam[:k],
        explained_share=lam[:k] / total if total > 0 else np.zeros(k),
        max_abs_loading=np.abs(cols).max(axis=0), l1_norm=np.abs(cols).sum(axis=0),
        inverse_participation_ratio=ipr, participation_ratio=1.0 / ipr, entropy=entropy,
    )


# --------------------------------------------------------------------------- risk attribution


@dataclass(frozen=True)
class RiskContribution:
    volatility: float
    marginal: FloatArray  # d volatility / d weight
    component: FloatArray  # weight * marginal; sums to the volatility (Euler)
    share: FloatArray  # component / volatility; sums to 1


def risk_contribution(weights: object, cov: object) -> RiskContribution:
    c = np.asarray(cov, dtype=float)
    w = _check_weights(weights, c.shape[0])
    var = float(w @ c @ w)
    if not var > 0.0:
        raise ValueError("portfolio variance is not positive")
    vol = math.sqrt(var)
    marginal = (c @ w) / vol
    component = w * marginal
    return RiskContribution(vol, marginal, component, component / vol)


@dataclass(frozen=True)
class EigenfactorRisk:
    """Portfolio variance split across the eigenfactors of one correlation matrix."""

    eigenvalues: FloatArray
    exposure: FloatArray  # V' (vol * w)
    variance: FloatArray  # eigenvalue * exposure^2
    share: FloatArray
    total: float  # equals w' (D C D) w; checked
    residual: float  # total minus the direct quadratic form, kept for the record


def eigenfactor_risk(corr: object, vol: object, weights: object, *, tol: float = 1e-8) -> EigenfactorRisk:
    """Variance by eigenfactor: ``sum_k lambda_k (v_k . (D w))^2 = w' D C D w``.

    Takes the matrix itself, not its eigenpairs, so the spectrum can never come from a different
    matrix than the one the volatility is measured on (clipping changes eigenvalues, and the unit-
    diagonal rescale that follows changes them again). Weights are scaled into correlation space by
    the volatilities first. A decomposition that does not reconcile raises instead of reporting.
    """
    c = np.asarray(corr, dtype=float)
    n = c.shape[0]
    d = np.asarray(vol, dtype=float)
    if d.shape != (n,) or not (np.isfinite(d).all() and (d > 0).all()):
        raise ValueError(f"vol must be {n} positive finite numbers")
    w_scaled = d * _check_weights(weights, n)
    lam, vecs = eigendecompose(c)
    exposure = vecs.T @ w_scaled
    variance = lam * exposure**2
    total = float(variance.sum())
    direct = float(w_scaled @ c @ w_scaled)
    if not total > 0.0 or abs(total - direct) > tol * max(abs(direct), 1e-300):
        raise ValueError(f"eigenfactor variances ({total!r}) do not reconcile with the portfolio variance ({direct!r})")
    return EigenfactorRisk(lam, exposure, variance, variance / total, total, total - direct)


# --------------------------------------------------------------------------- comparison runs


@dataclass(frozen=True)
class MPRun:
    """One Marchenko-Pastur treatment of the same correlation matrix. A comparison, never a choice."""

    label: str
    spec: MPSpec | None  # None for the bare-bounds count, which builds no matrix
    sigma2: float
    lambda_plus: float
    threshold: float
    n_signal: int
    clean_corr: FloatArray | None


def _run(label: str, corr: FloatArray, n_obs: int, spec: MPSpec) -> MPRun:
    d = denoise(corr, n_obs, spec)
    return MPRun(label, spec, d.sigma2, d.lambda_plus, d.threshold,
                 int((d.eigenvalues > d.threshold).sum()), d.clean_corr)


def compare_mp_estimators(corr: object, n_obs: int) -> dict[str, MPRun]:
    """The three implementations the scope found, on one matrix, each labelled for what it is.

    ``pinned``        the estimator in use (:data:`PINNED_MP`)
    ``clip_to_mean``  sp100_mp_portfolio_risk's filter: the same function with no Tracy-Widom margin
    ``bare_bounds``   financial_spectral_filters' bounds at sigma2 = 1: a count, no matrix

    Only ``pinned`` may be written to the graph; the others exist so the disagreement is on record.
    """
    c = np.asarray(corr, dtype=float)
    vals, _ = eigendecompose(c)
    q = len(vals) / n_obs
    bare_hi = (1.0 + math.sqrt(q)) ** 2
    return {
        "pinned": _run("pinned", c, n_obs, PINNED_MP),
        "clip_to_mean": _run("clip_to_mean", c, n_obs, MPSpec(tw_sigmas=0.0)),
        "bare_bounds": MPRun("bare_bounds", None, 1.0, bare_hi, bare_hi, int((vals > bare_hi).sum()), None),
    }


def relative_frobenius_distance(a: FloatArray, b: FloatArray) -> float:
    """``|a - b|_F / |b|_F``: how far one denoised matrix sits from another."""
    return float(np.linalg.norm(a - b) / np.linalg.norm(b))
