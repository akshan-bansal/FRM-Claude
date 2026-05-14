"""Historical Value-at-Risk (95% by default)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def historical_var(returns: pd.Series, confidence: float = 0.95) -> float:
    """One-tail historical VaR over the provided return series.

    Returns a *positive* number representing the loss threshold not exceeded
    with the given confidence. E.g. VaR=0.03 means "we are 95% confident the
    one-period loss won't exceed 3%".
    """
    if returns.empty:
        return 0.0
    quantile = 1.0 - confidence
    return float(-np.percentile(returns.dropna().to_numpy(), quantile * 100.0))
