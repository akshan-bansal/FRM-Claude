"""Kalman filter for a *time-varying* hedge ratio between two cointegrated legs.

The classic static pairs trade assumes a fixed hedge ratio (``spread = y - beta*x`` with
``beta`` constant). Real relationships drift, so the fixed ratio decays and the spread stops
being mean-reverting. This models the ratio as a hidden state that follows a random walk and
updates it one observation at a time (Chan, *Algorithmic Trading*, ch. 3):

    state    b_t = [alpha_t, beta_t]           evolves as  b_t = b_{t-1} + w,  w ~ N(0, Q)
    observe  y_t = [1, x_t] . b_t + v,                      v ~ N(0, R)

The one-step-ahead prediction error ``e_t = y_t - [1, x_t] . b_pred`` is the live spread, and
``e_t / sqrt(S_t)`` (S_t = innovation variance) is a self-normalizing z-score. Both use only
information through ``t`` (b_pred comes from data up to ``t-1``), so the filter is causal — no
lookahead — which is exactly what a trading signal needs.

Q is parameterized by a single ``delta`` in (0, 1): ``Q = delta/(1-delta) * I``. Larger delta =
faster-adapting ratio (noisier); smaller = steadier. ``R`` is the observation-noise variance.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True)
class KalmanHedgeState:
    """Per-bar filter output, all length-N and aligned to the input series.

    ``alpha``/``beta`` are the filtered intercept and hedge ratio; ``spread`` is the one-step
    prediction error ``e_t``; ``spread_std`` is ``sqrt(S_t)``; ``zscore`` is ``e_t/sqrt(S_t)``.
    The first ``warmup`` entries are unreliable while the covariance settles.
    """

    alpha: FloatArray
    beta: FloatArray
    spread: FloatArray
    spread_std: FloatArray
    zscore: FloatArray
    warmup: int


class KalmanHedge:
    """Recursive least-squares hedge-ratio filter. ``delta`` sets state-drift, ``r_obs`` the
    observation noise, ``warmup`` how many bars to flag as unsettled."""

    def __init__(self, delta: float = 1e-4, r_obs: float = 1e-3, warmup: int = 20) -> None:
        if not 0.0 < delta < 1.0:
            raise ValueError("delta must be in (0, 1)")
        if r_obs <= 0.0:
            raise ValueError("r_obs must be positive")
        self.delta = delta
        self.r_obs = r_obs
        self.warmup = warmup

    def filter(self, y: npt.ArrayLike, x: npt.ArrayLike) -> KalmanHedgeState:
        """Run the filter over dependent leg ``y`` against independent leg ``x`` (same length).

        Pass price levels (or log-prices) for the two legs; the caller decides. Returns a
        :class:`KalmanHedgeState` with the filtered ratio and the standardized spread.
        """
        yv = np.asarray(y, dtype=np.float64)
        xv = np.asarray(x, dtype=np.float64)
        if yv.shape != xv.shape or yv.ndim != 1:
            raise ValueError("y and x must be 1-D arrays of the same length")
        n = yv.shape[0]

        q = self.delta / (1.0 - self.delta)
        state_cov = np.eye(2) * q          # Q
        b = np.zeros(2)                    # [alpha, beta]
        p = np.eye(2) * 1.0                # posterior covariance, diffuse-ish prior

        alpha = np.full(n, np.nan)
        beta = np.full(n, np.nan)
        spread = np.full(n, np.nan)
        spread_std = np.full(n, np.nan)
        zscore = np.full(n, np.nan)

        for t in range(n):
            h = np.array([1.0, xv[t]])          # design row [1, x_t]
            p_pred = p + state_cov              # predict covariance (state is a random walk)
            y_hat = float(h @ b)                # one-step prediction using b_{t-1}
            e = float(yv[t] - y_hat)            # innovation = live spread
            s = float(h @ p_pred @ h) + self.r_obs   # innovation variance
            k = (p_pred @ h) / s                # Kalman gain
            b = b + k * e                       # posterior state
            p = p_pred - np.outer(k, h) @ p_pred

            alpha[t] = b[0]
            beta[t] = b[1]
            spread[t] = e
            std = float(np.sqrt(s))
            spread_std[t] = std
            zscore[t] = e / std if std > 0.0 else 0.0

        return KalmanHedgeState(alpha=alpha, beta=beta, spread=spread,
                                spread_std=spread_std, zscore=zscore, warmup=self.warmup)
