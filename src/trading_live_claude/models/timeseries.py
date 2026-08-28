"""Rolling ARIMA return forecasts and GARCH volatility forecasts, plus a moving-average ladder.

These feed the ``arima_garch`` strategy: ARIMA gives a one-step-ahead *direction* on returns, a
ladder of moving averages of increasing degree gives the *trend* backdrop, and GARCH gives a
one-step *volatility* forecast used to scale conviction and stand aside in turbulent regimes.

Everything is causal. Both forecasters refit their parameters only every ``refit_every`` bars
(fitting ARIMA/GARCH every bar is far too slow for a backtest) and then propagate cheaply between
refits: ARIMA via statsmodels' ``append(refit=False)``, GARCH via the closed-form recursion
``sigma2_t = omega + alpha*r_{t-1}^2 + beta*sigma2_{t-1}``. A forecast at bar ``t`` uses only data
through ``t-1``, so there is no lookahead.
"""
from __future__ import annotations

import warnings

import numpy as np
import numpy.typing as npt
import pandas as pd

FloatArray = npt.NDArray[np.float64]


def ma_ladder(price: pd.Series, windows: tuple[int, ...] = (10, 20, 50, 100, 200)) -> pd.Series:
    """Trend score in [-1, 1]: the fraction of the MA ladder the price sits above, signed.

    A "degrees of moving averages" backdrop — price above all rungs → +1 (strong uptrend), below
    all → -1. Shifted by one bar so the score at ``t`` uses only closes through ``t-1``.
    """
    above = pd.DataFrame({w: (price > price.rolling(w).mean()).astype(float) for w in windows})
    score = 2.0 * above.mean(axis=1) - 1.0
    return score.shift(1).fillna(0.0)


def rolling_arima_forecast(returns: npt.ArrayLike, *, order: tuple[int, int, int] = (1, 0, 1),
                           window: int = 250, refit_every: int = 21) -> FloatArray:
    """One-step-ahead ARIMA forecast of ``returns`` at each bar (NaN during warm-up).

    Refit on the trailing ``window`` every ``refit_every`` bars; between refits, extend the fitted
    model with each new observation via ``append(refit=False)`` and forecast one step.
    """
    from statsmodels.tsa.arima.model import ARIMA

    r = np.asarray(returns, dtype=np.float64)
    n = r.shape[0]
    out = np.full(n, np.nan)
    if n <= window + 2:
        return out
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = None
        for t in range(window, n):
            if res is None or (t - window) % refit_every == 0:
                res = ARIMA(r[t - window:t], order=order).fit()
            else:
                res = res.append(r[t - 1:t], refit=False)
            try:
                out[t] = float(res.forecast(steps=1)[0])
            except Exception:
                out[t] = 0.0
    return out


def rolling_garch_vol(returns: npt.ArrayLike, *, window: int = 250, refit_every: int = 42,
                      scale: float = 100.0) -> FloatArray:
    """One-step-ahead GARCH(1,1) conditional volatility (same units as ``returns``), NaN in warm-up.

    Refit GARCH params on the trailing ``window`` every ``refit_every`` bars, then propagate the
    conditional variance recursively each bar — exact given the params, and cheap.
    """
    from arch import arch_model

    r = np.asarray(returns, dtype=np.float64) * scale  # arch prefers percent-scale returns
    n = r.shape[0]
    out = np.full(n, np.nan)
    if n <= window + 2:
        return out
    omega, alpha, beta = 0.0, 0.0, 0.0
    fitted = False
    sigma2 = float(np.var(r[:window]))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for t in range(window, n):
            if not fitted or (t - window) % refit_every == 0:
                try:
                    fit = arch_model(r[t - window:t], mean="Zero", vol="GARCH", p=1, q=1).fit(disp="off")
                    omega = float(fit.params["omega"])
                    alpha = float(fit.params["alpha[1]"])
                    beta = float(fit.params["beta[1]"])
                    sigma2 = float(fit.conditional_volatility[-1] ** 2)
                except Exception:
                    omega, alpha, beta = float(np.var(r[t - window:t])), 0.0, 0.0
                fitted = True
            sigma2 = omega + alpha * r[t - 1] ** 2 + beta * sigma2  # one-step-ahead conditional variance
            out[t] = float(np.sqrt(max(sigma2, 1e-12)) / scale)
    return out
