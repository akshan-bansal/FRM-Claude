"""Tail-risk measures — CVaR / Expected Shortfall and friends.

Point risk (max drawdown, stdev) says little about the *shape* of the loss tail.
These functions characterize how bad the bad days are, so scoring can squeeze out
loss probability rather than just average volatility:

  * ``value_at_risk``       — the alpha-quantile loss (historical VaR)
  * ``expected_shortfall``  — CVaR/ES: the mean loss *beyond* VaR (the tail average)
  * ``cornish_fisher_var``  — modified VaR that inflates the tail for skew/kurtosis
  * ``conditional_drawdown_at_risk`` — CDaR: mean of the worst drawdowns
  * ``downside_deviation`` / ``ulcer_index`` / ``tail_ratio`` / ``omega_ratio``
  * ``loss_probability``    — P(return < threshold)

All loss measures return a *positive* number = magnitude of loss (e.g. 0.03 = 3%).
``alpha`` is the tail probability (0.05 = the worst 5%).
"""
from __future__ import annotations

from statistics import NormalDist

import numpy as np
import pandas as pd


def _clean(returns: pd.Series) -> np.ndarray:
    return np.asarray(returns.dropna().to_numpy(), dtype=float)


def value_at_risk(returns: pd.Series, alpha: float = 0.05) -> float:
    """Historical VaR at tail probability ``alpha`` (positive = loss magnitude)."""
    r = _clean(returns)
    if r.size == 0:
        return 0.0
    return float(-np.percentile(r, alpha * 100.0))


def expected_shortfall(returns: pd.Series, alpha: float = 0.05) -> float:
    """CVaR / Expected Shortfall: mean loss in the worst ``alpha`` tail (positive).

    The average of every return at or below the alpha-quantile — a coherent risk
    measure that, unlike VaR, sees how deep the tail actually goes.
    """
    r = _clean(returns)
    if r.size == 0:
        return 0.0
    threshold = np.percentile(r, alpha * 100.0)
    tail = r[r <= threshold]
    if tail.size == 0:
        return float(-threshold)
    return float(-tail.mean())


def cornish_fisher_var(returns: pd.Series, alpha: float = 0.05) -> float:
    """Modified (Cornish-Fisher) VaR adjusting the normal quantile for skew/kurtosis.

    Fat-tailed, negatively-skewed return streams get a *larger* VaR than the Gaussian
    assumption would give — the point is to stop under-stating tail loss.
    """
    r = _clean(returns)
    if r.size < 4:
        return value_at_risk(returns, alpha)
    mu, sigma = float(r.mean()), float(r.std(ddof=0))
    if sigma == 0.0:
        return 0.0
    z = NormalDist().inv_cdf(alpha)
    s = float(((r - mu) ** 3).mean() / sigma**3)          # skewness
    k = float(((r - mu) ** 4).mean() / sigma**4 - 3.0)    # excess kurtosis
    z_cf = (
        z
        + (z**2 - 1) * s / 6.0
        + (z**3 - 3 * z) * k / 24.0
        - (2 * z**3 - 5 * z) * s**2 / 36.0
    )
    return float(-(mu + z_cf * sigma))


def downside_deviation(returns: pd.Series, mar: float = 0.0) -> float:
    """Target semi-deviation below ``mar`` (RMS of shortfalls over ALL observations)."""
    r = _clean(returns)
    if r.size == 0:
        return 0.0
    downside = np.minimum(r - mar, 0.0)  # upside contributes 0, divide by total N
    return float(np.sqrt((downside**2).mean()))


def ulcer_index(returns: pd.Series) -> float:
    """Ulcer index: RMS of the drawdown path (depth AND duration of underwater time)."""
    r = _clean(returns)
    if r.size == 0:
        return 0.0
    equity = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak
    return float(np.sqrt((drawdown**2).mean()))


def conditional_drawdown_at_risk(returns: pd.Series, alpha: float = 0.05) -> float:
    """CDaR: mean of the worst ``alpha`` fraction of drawdowns (positive)."""
    r = _clean(returns)
    if r.size == 0:
        return 0.0
    equity = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(equity)
    drawdowns = (equity - peak) / peak  # <= 0
    threshold = np.percentile(drawdowns, alpha * 100.0)
    tail = drawdowns[drawdowns <= threshold]
    if tail.size == 0:
        return float(-threshold)
    return float(-tail.mean())


def tail_ratio(returns: pd.Series, alpha: float = 0.05) -> float:
    """Right-tail magnitude / left-tail magnitude. >1 means gains outrun losses."""
    r = _clean(returns)
    if r.size == 0:
        return 0.0
    right = abs(np.percentile(r, (1 - alpha) * 100.0))
    left = abs(np.percentile(r, alpha * 100.0))
    return float(right / left) if left > 0 else 0.0


def omega_ratio(returns: pd.Series, threshold: float = 0.0) -> float:
    """Omega: sum of gains above ``threshold`` / sum of losses below it."""
    r = _clean(returns)
    gains = (r[r > threshold] - threshold).sum()
    losses = (threshold - r[r <= threshold]).sum()
    return float(gains / losses) if losses > 0 else float("inf") if gains > 0 else 0.0


def loss_probability(returns: pd.Series, threshold: float = 0.0) -> float:
    """P(return < threshold) — the raw loss probability in [0, 1]."""
    r = _clean(returns)
    if r.size == 0:
        return 0.0
    return float((r < threshold).mean())
