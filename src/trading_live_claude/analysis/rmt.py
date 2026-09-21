"""Random-matrix-theory denoising of a return correlation matrix (Marchenko-Pastur filter).

For ``N`` assets observed over ``T`` bars, the eigenvalues of the sample correlation matrix of
*pure noise* fall inside the Marchenko-Pastur band ``[lambda_-, lambda_+]`` with
``lambda_+- = sigma2 * (1 +- sqrt(q))**2`` and ``q = N / T``. Eigenvalues above ``lambda_+`` carry
real cross-asset structure (the first is almost always the "market mode"); the rest are
indistinguishable from estimation noise.

``denoise_correlation`` keeps the signal eigenpairs, replaces the noise eigenvalues, rebuilds the
matrix, and rescales it back to a unit diagonal. Eigenvectors are never modified — only the
spectrum is — so anything that projects returns onto eigenvectors (``factor_returns``) is
unaffected by *how* the noise is replaced, apart from the ``sqrt(lambda)`` normalization.

Upper edge. ``sigma2`` is not 1 for real markets: the market mode soaks up a large share of total
variance, leaving less for the noise bulk. It is estimated in one pass (Laloux et al. 1999; Plerou
et al. 2002): eigenvalues above the ``sigma2 = 1`` edge are provisionally signal, and ``sigma2`` is
the mean of the rest (the MP law has mean ``sigma2``). This is deliberately NOT iterated to a fixed
point — iterating is biased toward collapse: each pass that reclassifies the top of the bulk as
signal lowers the bulk mean, which lowers ``lambda_+``, which reclassifies the next eigenvalue. On
the 19-asset crypto basket that ran from 1 to 10 "signal" modes. With finite ``N`` the largest bulk
eigenvalue also routinely pokes past the asymptotic ``lambda_+`` (Tracy-Widom fluctuations), so a
signal eigenvalue must clear ``lambda_+`` by ``tw_sigmas`` TW scale units (Johnstone 2001); 2.0 is
roughly the TW1 99th percentile.

Lower edge. Eigenvalues *below* ``lambda_-`` are also outside the noise band: they are directions of
near-collinearity (tight pairs, an asset and its near-clone). ``lower_cut`` sets where the lower
boundary sits. Eigenvalues below it are kept as structure; everything between it and the upper
threshold is noise and gets replaced. ``lower_cut = 0`` (the default) treats the whole low end as
noise; ``lower_edge()`` gives the MP-consistent boundary ``lambda_- - tw_sigmas * TW_lower``.
Moving the cut from that edge down toward zero "extends the lower boundary": progressively more
low-level eigenvalues are classed as noise and removed.

Noise shape. The replaced eigenvalues can be set to (all trace-preserving, rank order kept):
  * ``constant``  — every noise eigenvalue -> their mean (Laloux constant-residual; a delta)
  * ``uniform``   — evenly spread quantiles across ``[lambda_-, lambda_+]``
  * ``arcsine``   — arcsine-law quantiles on the band (mass piled toward both edges)
  * ``binomial``  — two-point: lower half at ``lambda_-``, upper half at ``lambda_+``
  * ``mp``        — Marchenko-Pastur quantiles (what pure noise would have produced)
In order of edge weight: constant < mp < uniform < arcsine < binomial.

Pure numpy, no I/O. Lookahead is the *caller's* concern: fit ``fit_rmt`` on a training window
only, then use ``RMTResult.factor_returns`` to project later bars through the frozen eigenvectors.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
import pandas as pd

FloatArray = npt.NDArray[np.float64]
NoiseShape = Literal["constant", "uniform", "arcsine", "binomial", "mp"]
NOISE_SHAPES: tuple[NoiseShape, ...] = ("constant", "mp", "uniform", "arcsine", "binomial")


def marchenko_pastur_bounds(q: float, sigma2: float = 1.0) -> tuple[float, float]:
    """``(lambda_-, lambda_+)`` for aspect ratio ``q = N / T`` (must be in ``(0, 1]``)."""
    if not 0.0 < q <= 1.0:
        raise ValueError(f"q = N/T must be in (0, 1], got {q}")
    root = np.sqrt(q)
    return float(sigma2 * (1.0 - root) ** 2), float(sigma2 * (1.0 + root) ** 2)


def marchenko_pastur_pdf(x: npt.ArrayLike, q: float, sigma2: float = 1.0) -> FloatArray:
    """Marchenko-Pastur eigenvalue density at ``x``; zero outside the band."""
    xs = np.asarray(x, dtype=np.float64)
    lo, hi = marchenko_pastur_bounds(q, sigma2)
    inside = (xs > lo) & (xs < hi)
    out = np.zeros_like(xs)
    xi = xs[inside]
    out[inside] = np.sqrt((hi - xi) * (xi - lo)) / (2.0 * np.pi * q * sigma2 * xi)
    return out


def _sorted_eigh(corr: FloatArray) -> tuple[FloatArray, FloatArray]:
    """Eigenpairs sorted by eigenvalue descending, each eigenvector signed so its components sum
    to >= 0. The sign rule makes the market mode read as "long the basket" and keeps the result
    deterministic across runs and platforms."""
    vals, vecs = np.linalg.eigh(corr)
    order = np.argsort(vals)[::-1]
    vals, vecs = vals[order], vecs[:, order]
    signs = np.where(vecs.sum(axis=0) < 0.0, -1.0, 1.0)
    return vals, vecs * signs


def tracy_widom_scale(n: int, t: int, sigma2: float = 1.0) -> float:
    """Johnstone's (2001) scale of the largest-eigenvalue fluctuation for an ``N x N`` sample
    correlation built from ``T`` bars, in eigenvalue units."""
    a, b = np.sqrt(t - 1.0), np.sqrt(float(n))
    return float(sigma2 * (a + b) * (1.0 / a + 1.0 / b) ** (1.0 / 3.0) / t)


def tracy_widom_scale_lower(n: int, t: int, sigma2: float = 1.0) -> float:
    """Scale of the smallest-eigenvalue fluctuation (Ma 2012; valid for ``N < T``)."""
    a, b = np.sqrt(float(t)), np.sqrt(float(n))
    return float(sigma2 * (a - b) * (1.0 / b - 1.0 / a) ** (1.0 / 3.0) / t)


def signal_threshold(n: int, t: int, sigma2: float, tw_sigmas: float) -> float:
    """``lambda_+`` plus a finite-sample Tracy-Widom margin."""
    return marchenko_pastur_bounds(n / t, sigma2)[1] + tw_sigmas * tracy_widom_scale(n, t, sigma2)


def estimate_noise_sigma2(eigenvalues: FloatArray, n_obs: int, *,
                          tw_sigmas: float = 2.0) -> float:
    """Noise-bulk variance, one pass: mean of the eigenvalues below the ``sigma2 = 1`` cut."""
    noise = eigenvalues[eigenvalues <= signal_threshold(len(eigenvalues), n_obs, 1.0, tw_sigmas)]
    return float(noise.mean()) if len(noise) else 1.0


def shape_quantiles(shape: NoiseShape, m: int, lo: float, hi: float, q: float,
                    sigma2: float) -> FloatArray:
    """``m`` ascending values distributed per ``shape`` on ``[lo, hi]`` (midpoint quantiles)."""
    p = (np.arange(m) + 0.5) / m
    if shape == "constant":
        return np.full(m, (lo + hi) / 2.0)  # level is irrelevant: rescaled to the trace later
    if shape == "uniform":
        return lo + (hi - lo) * p
    if shape == "arcsine":
        return lo + (hi - lo) * np.sin(np.pi * p / 2.0) ** 2
    if shape == "binomial":
        return np.where(p < 0.5, lo, hi).astype(np.float64)
    if shape == "mp":
        grid = np.linspace(lo, hi, 4001)
        pdf = marchenko_pastur_pdf(grid, q, sigma2)
        cdf = np.concatenate([[0.0], np.cumsum((pdf[1:] + pdf[:-1]) / 2.0 * np.diff(grid))])
        cdf /= cdf[-1]
        return np.interp(p, cdf, grid)
    raise ValueError(f"unknown noise shape {shape!r}")


@dataclass(frozen=True)
class Denoised:
    clean_corr: FloatArray
    eigenvalues: FloatArray          # raw spectrum, descending
    eigenvectors: FloatArray
    cleaned_eigenvalues: FloatArray  # spectrum after replacement, before unit-diagonal rescale
    sigma2: float
    lambda_minus: float
    lambda_plus: float
    threshold: float                 # upper signal cut
    lower_cut: float                 # eigenvalues strictly below are kept as low-end structure


def denoise_correlation(corr: FloatArray, n_obs: int, *, tw_sigmas: float = 2.0,
                        lower_cut: float = 0.0, noise_shape: NoiseShape = "constant") -> Denoised:
    """Marchenko-Pastur filter a correlation matrix estimated from ``n_obs`` bars."""
    n = corr.shape[0]
    vals, vecs = _sorted_eigh(corr)
    sigma2 = estimate_noise_sigma2(vals, n_obs, tw_sigmas=tw_sigmas)
    lo, hi = marchenko_pastur_bounds(n / n_obs, sigma2)
    thr = signal_threshold(n, n_obs, sigma2, tw_sigmas)
    noise = (vals <= thr) & (vals >= lower_cut)
    cleaned = vals.copy()
    m = int(noise.sum())
    if m:
        target = shape_quantiles(noise_shape, m, lo, hi, n / n_obs, sigma2)
        target = target * (vals[noise].sum() / target.sum())       # preserve the noise trace
        idx = np.flatnonzero(noise)                                 # descending order of vals
        cleaned[idx] = np.sort(target)[::-1]                        # keep rank order
    rebuilt = (vecs * cleaned) @ vecs.T
    d = np.sqrt(np.diag(rebuilt))
    clean = rebuilt / np.outer(d, d)
    np.fill_diagonal(clean, 1.0)
    return Denoised(clean, vals, vecs, cleaned, sigma2, lo, hi, thr, lower_cut)


def volume_weights(dollar_volume: pd.DataFrame, power: float = 1.0) -> FloatArray:
    """Per-asset weight from mean dollar volume over the given window, ``^power``, mean 1."""
    dv = dollar_volume.mean(axis=0).to_numpy(dtype=np.float64)
    if (dv <= 0).any() or not np.isfinite(dv).all():
        raise ValueError("dollar volume must be positive and finite for every asset")
    w: FloatArray = dv ** power
    out: FloatArray = w / w.mean()
    return out


@dataclass(frozen=True)
class RMTResult:
    symbols: tuple[str, ...]
    n_obs: int
    mean: FloatArray            # per-asset training mean of returns, used to standardize later bars
    std: FloatArray             # per-asset training std
    raw_corr: FloatArray
    clean_corr: FloatArray
    eigenvalues: FloatArray     # descending
    eigenvectors: FloatArray    # columns, aligned with eigenvalues
    cleaned_eigenvalues: FloatArray
    sigma2: float
    lambda_minus: float
    lambda_plus: float          # asymptotic MP edge
    threshold: float            # lambda_plus + Tracy-Widom margin; eigenvalues above are signal
    lower_cut: float
    noise_shape: NoiseShape = "constant"
    eig1_weights: FloatArray | None = None  # volume weights applied to the market-mode direction

    @property
    def q(self) -> float:
        return len(self.symbols) / self.n_obs

    @property
    def n_signal(self) -> int:
        return int((self.eigenvalues > self.threshold).sum())

    @property
    def n_lower_kept(self) -> int:
        return int((self.eigenvalues < self.lower_cut).sum())

    @property
    def signal_share(self) -> float:
        """Fraction of total variance (trace = N) carried by the upper signal eigenvalues."""
        return float(self.eigenvalues[: self.n_signal].sum() / len(self.symbols))

    @property
    def spectral_snr(self) -> float:
        """In-sample variance ratio: upper-signal eigenvalues / eigenvalues classed as noise."""
        noise = (self.eigenvalues <= self.threshold) & (self.eigenvalues >= self.lower_cut)
        return float(self.eigenvalues[: self.n_signal].sum() / max(self.eigenvalues[noise].sum(),
                                                                    1e-12))

    def lower_edge(self, tw_sigmas: float = 2.0) -> float:
        """MP-consistent lower boundary ``lambda_- - tw_sigmas * TW_lower`` (floored at 0)."""
        n = len(self.symbols)
        return max(self.lambda_minus - tw_sigmas * tracy_widom_scale_lower(n, self.n_obs,
                                                                           self.sigma2), 0.0)

    def directions(self, k: int) -> FloatArray:
        """``(N, k)`` unit-norm directions: the top-k eigenvectors, with column 0 replaced by the
        volume-weighted market mode when ``eig1_weights`` is set. The weighted column is no longer
        an exact eigenvector (nor orthogonal to the rest) — it is the market mode tilted toward
        where the trading actually happens."""
        u = self.eigenvectors[:, :k].copy()
        if self.eig1_weights is not None:
            v = u[:, 0] * self.eig1_weights
            u[:, 0] = v / np.linalg.norm(v)
        return u

    def mode_variances(self, k: int) -> FloatArray:
        """In-sample variance of each direction: ``u' C u`` (= eigenvalue for pure eigenvectors)."""
        u = self.directions(k)
        var: FloatArray = np.einsum("ik,ij,jk->k", u, self.raw_corr, u)
        return var

    def embed(self, k: int = 6) -> FloatArray:
        """``(N, k)`` coordinates: loading on each direction x ``sqrt(variance)``, so axis length
        reflects how much variance that mode explains. Axes past ``n_signal`` sit inside the noise
        band — plot them, but don't read structure into them."""
        k = min(k, len(self.eigenvalues))
        return self.directions(k) * np.sqrt(self.mode_variances(k))

    def factor_returns(self, returns: pd.DataFrame, k: int | None = None) -> pd.DataFrame:
        """Project ``returns`` (columns == ``symbols``) onto the top-``k`` directions (default: the
        upper signal modes, at least one).

        Standardizes with the *training* mean/std and divides by each direction's in-sample std so
        every factor has unit variance in-sample. Row ``t`` depends only on row ``t`` of
        ``returns``, so no future information enters as long as the fit window precedes the bars
        being projected.
        """
        if tuple(returns.columns) != self.symbols:
            raise ValueError("returns columns must match the fitted symbols, in order")
        k = min(k if k is not None else max(self.n_signal, 1), len(self.symbols))
        z = (returns.to_numpy(dtype=np.float64) - self.mean) / self.std
        f = z @ self.directions(k) / np.sqrt(self.mode_variances(k))
        return pd.DataFrame(f, index=returns.index, columns=[f"eig{i + 1}" for i in range(k)])


def fit_rmt(returns: pd.DataFrame, *, tw_sigmas: float = 2.0, lower_cut: float = 0.0,
            noise_shape: NoiseShape = "constant",
            eig1_weights: FloatArray | None = None) -> RMTResult:
    """Correlation matrix of ``returns`` (rows = bars, columns = assets) plus its MP-denoised twin.

    Rows with any NaN are dropped. Requires more bars than assets (``q <= 1``)."""
    clean_rows = returns.dropna()
    t, n = clean_rows.shape
    if t <= n:
        raise ValueError(f"need more bars than assets for MP filtering, got T={t}, N={n}")
    if eig1_weights is not None and eig1_weights.shape != (n,):
        raise ValueError(f"eig1_weights must have shape ({n},)")
    x = clean_rows.to_numpy(dtype=np.float64)
    mean, std = x.mean(axis=0), x.std(axis=0, ddof=0)
    if (std == 0).any():
        flat = [s for s, sd in zip(clean_rows.columns, std, strict=True) if sd == 0]
        raise ValueError(f"zero-variance columns: {flat}")
    z = (x - mean) / std
    raw = (z.T @ z) / t
    d = denoise_correlation(raw, t, tw_sigmas=tw_sigmas, lower_cut=lower_cut,
                            noise_shape=noise_shape)
    return RMTResult(
        symbols=tuple(str(c) for c in clean_rows.columns), n_obs=t, mean=mean, std=std,
        raw_corr=raw, clean_corr=d.clean_corr, eigenvalues=d.eigenvalues,
        eigenvectors=d.eigenvectors, cleaned_eigenvalues=d.cleaned_eigenvalues, sigma2=d.sigma2,
        lambda_minus=d.lambda_minus, lambda_plus=d.lambda_plus, threshold=d.threshold,
        lower_cut=lower_cut, noise_shape=noise_shape, eig1_weights=eig1_weights,
    )


# ---- out-of-sample scoring of a correlation estimate ------------------------------------------


def gaussian_nll(corr: FloatArray, z: FloatArray) -> FloatArray:
    """Per-row Gaussian negative log-likelihood (constant dropped) of standardized returns ``z``
    under correlation ``corr``. A proper scoring rule: it punishes eigenvalues set too small
    (overconfident low-variance directions) as hard as ones set too large."""
    sign, logdet = np.linalg.slogdet(corr)
    if sign <= 0:
        raise ValueError("correlation matrix is not positive definite")
    solved = np.linalg.solve(corr, z.T).T
    nll: FloatArray = 0.5 * (logdet + np.einsum("ij,ij->i", z, solved))
    return nll


def min_variance_weights(corr: FloatArray, std: FloatArray) -> FloatArray:
    """Fully-invested minimum-variance weights for covariance ``D C D`` (shorts allowed)."""
    cov = corr * np.outer(std, std)
    w = np.asarray(np.linalg.solve(cov, np.ones(len(std))), dtype=np.float64)
    out: FloatArray = w / w.sum()
    return out
