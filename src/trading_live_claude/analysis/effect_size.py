"""Effect size and significance for a paired daily difference series (Diebold-Mariano style).

Given ``d_t = loss_A(t) - loss_B(t)`` on the same days, report the mean difference, a Newey-West
(Bartlett kernel) HAC standard error — daily losses are autocorrelated, so the iid SE overstates
significance — the resulting t-stat and two-sided normal p-value, and Cohen's ``d_z`` (mean / sd of
the paired differences), the scale-free effect size.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True)
class PairedEffect:
    mean_diff: float
    hac_se: float
    t_stat: float
    p_value: float
    cohens_dz: float
    n: int
    lags: int

    def ci95(self) -> tuple[float, float]:
        return self.mean_diff - 1.96 * self.hac_se, self.mean_diff + 1.96 * self.hac_se


def newey_west_lags(n: int) -> int:
    """Standard automatic bandwidth ``floor(4 * (n/100)^(2/9))``."""
    return math.floor(4.0 * float((n / 100.0) ** (2.0 / 9.0)))


def paired_effect(diff: npt.ArrayLike, lags: int | None = None) -> PairedEffect:
    d = np.asarray(diff, dtype=np.float64)
    d = d[np.isfinite(d)]
    n = len(d)
    if n < 3:
        raise ValueError("need at least 3 paired observations")
    lags = newey_west_lags(n) if lags is None else lags
    mu = float(d.mean())
    e = d - mu
    lrv = float(e @ e) / n
    for k in range(1, lags + 1):
        lrv += 2.0 * (1.0 - k / (lags + 1.0)) * float(e[k:] @ e[:-k]) / n
    se = math.sqrt(max(lrv, 0.0) / n)
    t = mu / se if se > 0 else 0.0
    p = math.erfc(abs(t) / math.sqrt(2.0))
    sd = float(d.std(ddof=1))
    return PairedEffect(mu, se, t, p, mu / sd if sd > 0 else 0.0, n, lags)
