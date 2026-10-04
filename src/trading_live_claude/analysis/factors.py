"""Factor models for L2: named factors, eigenfactors and the society factor.

The three kinds of ``factor`` node in ``HOMOGENEOUS_GRAPH_SCOPE.md`` section 2, as pure functions
that return plain data. Turning that data into graph edges is ``factor_edges.py``'s job, kept apart
so this module stays free of graph imports.

* ``named``    regression loadings of each asset on a SUPPLIED factor-return series (market, size,
               value, momentum, rates, vol). The repo has no such series; nothing here builds or
               downloads one. Feed it yours.
* ``eigen``    eigenfactors of the pinned Marchenko-Pastur-denoised correlation of asset returns.
               Loadings are taken from the eigenpairs of the FINAL denoised matrix, the one the
               volatility is read from, so per-factor variance reconciles with portfolio variance.
* ``society``  the top eigenvector of the symbol-by-symbol correlation of simulated agents' stances.
               Its eigenvalue share is the consensus concentration the reverse adapter calls ``herd``.

None of this says a factor is priced, persistent or tradeable. A loading is an estimate over the
window supplied; fitting that window is the caller's concern, and it should end before whatever the
loadings are later used on. Contract, same as the rest of ``analysis``: a ruler, no I/O, never
imported by execution, risk, brokers, strategies, monitor or the daemon.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from .rmt import fit_rmt
from .spectral import PINNED_MP, MPSpec, SpectrumReport, classify_spectrum, eigendecompose

FloatArray = NDArray[np.float64]

# Fewer simulated agents than this and the stance matrix has no usable spectrum (rank <= N - 1, top
# share dominated by estimation noise). Mirrors ``sim.society.MIN_AGENTS_FOR_SPECTRUM``; a test keeps
# the two equal, so changing one without the other is caught.
MIN_AGENTS_FOR_SPECTRUM = 30


# --------------------------------------------------------------------------- named factors


@dataclass(frozen=True)
class FactorFit:
    """OLS of each asset on a supplied factor series: ``r_i = alpha_i + beta_i . f + eps_i``."""

    assets: tuple[str, ...]
    factors: tuple[str, ...]
    alpha: FloatArray  # (N,)
    betas: FloatArray  # (N, K)
    t_betas: FloatArray  # (N, K)
    r2: FloatArray  # (N,)
    resid_var: FloatArray  # (N,) with the T - K - 1 degrees-of-freedom correction
    factor_cov: FloatArray  # (K, K), sample covariance of the factor series
    n_obs: int
    n_dropped: int  # rows removed for a NaN or inf in any series, counted rather than hidden


def fit_named_factors(returns: pd.DataFrame, factors: pd.DataFrame) -> FactorFit:
    """Regress every column of ``returns`` on every column of ``factors`` (with an intercept).

    The two frames must share an index exactly: this never re-aligns or interpolates, because a
    silent inner join would change which bars the loadings describe. Rows with any NaN or inf are
    dropped and counted in ``n_dropped``. Factors that are collinear (or constant) raise instead of
    having one quietly dropped.
    """
    if not returns.index.equals(factors.index):
        raise ValueError("returns and factor series must share the same index; this does not re-align them")
    clash = set(map(str, returns.columns)) & set(map(str, factors.columns))
    if clash:
        raise ValueError(f"a name is used for both an asset and a factor: {sorted(clash)}")
    joined = pd.concat([returns, factors], axis=1).replace([np.inf, -np.inf], np.nan)
    clean = joined.dropna()
    n_obs, k = len(clean), factors.shape[1]
    if n_obs < k + 3:
        raise ValueError(f"need at least {k + 3} complete bars for {k} factors; got {n_obs}")
    y = clean[list(returns.columns)].to_numpy(dtype=float)
    f = clean[list(factors.columns)].to_numpy(dtype=float)
    x = np.column_stack([np.ones(n_obs), f])
    if np.linalg.matrix_rank(x) < k + 1:
        raise ValueError("factor series are collinear or constant; drop or combine them first")

    coef = np.linalg.lstsq(x, y, rcond=None)[0]  # (K + 1, N)
    resid = y - x @ coef
    sse = (resid**2).sum(axis=0)
    sst = ((y - y.mean(axis=0)) ** 2).sum(axis=0)
    if not (sst > 0).all():
        raise ValueError("an asset has zero variance over the window; its R-squared is undefined")
    resid_var = sse / (n_obs - k - 1)
    betas = coef[1:].T
    se = np.sqrt(np.outer(np.diag(np.linalg.inv(x.T @ x))[1:], resid_var)).T
    with np.errstate(divide="ignore", invalid="ignore"):
        t_betas = betas / se
    return FactorFit(
        assets=tuple(str(c) for c in returns.columns), factors=tuple(str(c) for c in factors.columns),
        alpha=coef[0], betas=betas, t_betas=t_betas, r2=1.0 - sse / sst, resid_var=resid_var,
        factor_cov=np.atleast_2d(np.cov(f, rowvar=False, ddof=1)), n_obs=n_obs,
        n_dropped=len(joined) - n_obs,
    )


@dataclass(frozen=True)
class NamedFactorRisk:
    """Portfolio variance as systematic (by factor) plus idiosyncratic."""

    exposure: FloatArray  # (K,) portfolio beta to each factor
    factor_variance: FloatArray  # (K,) Euler contribution of each factor; sums to ``systematic``
    systematic: float
    idiosyncratic: float  # sum w_i^2 * resid_var_i: residuals are treated as uncorrelated
    total: float
    sample_variance_gap: float | None  # total minus the portfolio's sample variance, if supplied


def named_factor_risk(weights: Sequence[float] | FloatArray, fit: FactorFit,
                      sample_cov: FloatArray | None = None) -> NamedFactorRisk:
    """Split ``w' Sigma w`` into the factor model's pieces.

    Residual correlation is ignored, so ``total`` equals the portfolio's full sample variance only
    to the extent residuals really are uncorrelated. Pass ``sample_cov`` to record the gap.
    """
    w = np.asarray(weights, dtype=float)
    if w.shape != (len(fit.assets),) or not np.isfinite(w).all():
        raise ValueError(f"weights must be {len(fit.assets)} finite numbers; got shape {w.shape}")
    b = fit.betas.T @ w
    factor_variance = b * (fit.factor_cov @ b)
    systematic = float(factor_variance.sum())
    idio = float((w * w * fit.resid_var).sum())
    gap = None if sample_cov is None else systematic + idio - float(w @ np.asarray(sample_cov) @ w)
    return NamedFactorRisk(b, factor_variance, systematic, idio, systematic + idio, gap)


# --------------------------------------------------------------------------- eigenfactors


@dataclass(frozen=True)
class EigenFactorModel:
    """Eigenfactors of the pinned denoised correlation, with everything needed to audit the choice."""

    symbols: tuple[str, ...]
    n_obs: int
    n_signal: int  # structural eigenvalues above the pinned threshold
    eigenvalues: FloatArray  # (k,) of the final denoised matrix, descending
    loadings: FloatArray  # (N, k): v_j * sqrt(lambda_j), the correlation of each asset with factor j
    variance_share: FloatArray  # (k,) lambda_j / N
    clean_corr: FloatArray  # the final matrix; hand it to ``eigenfactor_risk`` with ``vol``
    vol: FloatArray  # (N,) per-asset standard deviation over the fit window
    spec: MPSpec
    sigma2: float
    lambda_plus: float
    threshold: float
    spectrum: SpectrumReport


def eigen_factor_model(returns: pd.DataFrame, spec: MPSpec = PINNED_MP, *, k: int | None = None) -> EigenFactorModel:
    """Fit the pinned estimator and keep the structural eigenfactors (all of them unless ``k`` is given).

    ``k`` may not exceed the structural count: a factor past it is indistinguishable from noise, and
    writing it down as a loading would be inventing one. A window with no structure returns zero
    factors, not one manufactured to have something to show.
    """
    r = fit_rmt(returns, tw_sigmas=spec.tw_sigmas, lower_cut=spec.lower_cut, noise_shape=spec.noise_shape)
    n = len(r.symbols)
    take = r.n_signal if k is None else k
    if not 0 <= take <= r.n_signal:
        raise ValueError(f"k must lie in [0, {r.n_signal}] (the structural eigenvalue count); got {k}")
    vals, vecs = eigendecompose(r.clean_corr)  # the FINAL matrix, so variance reconciles downstream
    lam = vals[:take]
    return EigenFactorModel(
        symbols=r.symbols, n_obs=r.n_obs, n_signal=r.n_signal, eigenvalues=lam,
        loadings=vecs[:, :take] * np.sqrt(np.maximum(lam, 0.0)), variance_share=lam / n,
        clean_corr=r.clean_corr, vol=r.std, spec=spec, sigma2=r.sigma2, lambda_plus=r.lambda_plus,
        threshold=r.threshold, spectrum=classify_spectrum(r.eigenvalues, r.n_obs, mp=spec),
    )


# --------------------------------------------------------------------------- society factor


@dataclass(frozen=True)
class SocietyFactor:
    """The direction a simulated society agrees on, and how concentrated that agreement is."""

    symbols: tuple[str, ...]  # symbols that had any spread across agents
    dropped: tuple[str, ...]  # symbols every agent scored identically: no correlation to speak of
    loadings: FloatArray  # (len(symbols),): top eigenvector * sqrt(top eigenvalue)
    eigenvalue: float
    share: float  # top eigenvalue / trace: the consensus concentration, in [1/N, 1]
    n_agents: int


def society_factor(stances: object, symbols: Sequence[str], *,
                   min_agents: int = MIN_AGENTS_FOR_SPECTRUM) -> SocietyFactor | None:
    """Top eigenpair of the symbol-by-symbol correlation of agent stances, or ``None``.

    ``stances`` is agents x symbols. ``None`` (never a guessed number) when the matrix cannot
    support a spectrum: not 2-D, fewer than ``min_agents`` agents, non-finite entries, or fewer than
    two symbols with any spread. Same refusals as ``sim.society.top_eigenvalue_share``, and the
    ``share`` agrees with it on every input a test tries.
    """
    if min_agents < 2:
        raise ValueError(f"min_agents must be >= 2, got {min_agents!r}")
    x = np.asarray(stances, dtype=float)
    if x.ndim != 2 or x.shape[0] < min_agents or not np.all(np.isfinite(x)):
        return None
    if len(symbols) != x.shape[1]:
        raise ValueError(f"{len(symbols)} symbols for {x.shape[1]} stance columns")
    keep = x.std(axis=0, ddof=1) > 1e-12
    if int(keep.sum()) < 2:
        return None
    corr = np.corrcoef(x[:, keep], rowvar=False)
    vals, vecs = eigendecompose(corr)
    total = float(vals.sum())
    if not total > 0.0:
        return None
    kept = tuple(str(s) for s, flag in zip(symbols, keep, strict=True) if flag)
    gone = tuple(str(s) for s, flag in zip(symbols, keep, strict=True) if not flag)
    return SocietyFactor(kept, gone, vecs[:, 0] * math.sqrt(max(float(vals[0]), 0.0)),
                         float(vals[0]), float(vals[0]) / total, int(x.shape[0]))
