"""Cointegration testing and mean-reversion diagnostics for pairs.

A Kalman hedge ratio only pays off when the two legs are actually cointegrated — otherwise the
"spread" is a random walk and the z-score signal is noise. ``engle_granger`` runs the standard
two-step test (OLS hedge ratio, then an ADF unit-root test on the residual); ``half_life``
estimates how many bars the spread takes to revert halfway, which sets a sane lookback and a
gate against spreads that revert too slowly to trade.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from statsmodels.tsa.stattools import adfuller


@dataclass(frozen=True)
class CointegrationResult:
    hedge_ratio: float      # OLS beta of y on x
    intercept: float
    adf_stat: float         # ADF statistic on the residual spread
    pvalue: float           # lower = more strongly stationary/cointegrated
    half_life: float        # bars to revert halfway (inf if non-reverting)
    cointegrated: bool      # pvalue <= alpha AND finite, positive half-life

    @property
    def tradeable(self) -> bool:
        return self.cointegrated and 0.0 < self.half_life < float("inf")


def _ols(y: npt.NDArray[np.float64], x: npt.NDArray[np.float64]) -> tuple[float, float, npt.NDArray[np.float64]]:
    """Return (beta, intercept, residuals) for y ~ intercept + beta*x."""
    design = np.column_stack([np.ones_like(x), x])
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    resid = y - design @ coef
    return float(coef[1]), float(coef[0]), resid


def half_life(spread: npt.ArrayLike) -> float:
    """Ornstein-Uhlenbeck half-life of mean reversion, in bars.

    Regress the spread change on its lagged level (``d s_t = k * s_{t-1} + c``); a negative
    ``k`` implies reversion with half-life ``-ln(2)/ln(1+k)``. Returns ``inf`` when the spread
    is not reverting (k >= 0), so callers can gate on ``isfinite``.
    """
    s = np.asarray(spread, dtype=np.float64)
    s = s[np.isfinite(s)]
    if s.shape[0] < 10:
        return float("inf")
    lag = s[:-1]
    delta = np.diff(s)
    k, _, _ = _ols(delta, lag)
    if k >= 0.0:
        return float("inf")
    hl = -np.log(2.0) / np.log1p(k)
    return float(hl) if np.isfinite(hl) and hl > 0.0 else float("inf")


def engle_granger(y: npt.ArrayLike, x: npt.ArrayLike, *, alpha: float = 0.05,
                  max_half_life: float = 252.0) -> CointegrationResult:
    """Two-step Engle-Granger cointegration test on legs ``y`` and ``x`` (price or log-price).

    ``alpha`` is the ADF significance threshold; ``max_half_life`` rejects pairs that revert too
    slowly to trade even if statistically cointegrated.
    """
    yv = np.asarray(y, dtype=np.float64)
    xv = np.asarray(x, dtype=np.float64)
    if yv.shape != xv.shape or yv.ndim != 1:
        raise ValueError("y and x must be 1-D arrays of the same length")
    beta, intercept, resid = _ols(yv, xv)
    adf = adfuller(resid, maxlag=1, autolag=None, result_object=True)
    adf_stat, pvalue = float(adf.statistic), float(adf.pvalue)
    hl = half_life(resid)
    cointegrated = bool(pvalue <= alpha and 0.0 < hl <= max_half_life)
    return CointegrationResult(hedge_ratio=beta, intercept=intercept, adf_stat=float(adf_stat),
                               pvalue=float(pvalue), half_life=hl, cointegrated=cointegrated)
