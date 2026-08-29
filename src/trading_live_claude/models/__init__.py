"""Quantitative primitives shared by strategies: Kalman filtering, cointegration tests,
and time-series (ARIMA / GARCH) forecasting. These are pure model code — no I/O, no broker,
no lookahead — so they can be unit-tested in isolation and composed by Strategy subclasses."""
from __future__ import annotations

from .cointegration import CointegrationResult, engle_granger, half_life
from .kalman import KalmanHedge, KalmanHedgeState
from .regime import RegimeClassifier, RegimeState

__all__ = [
    "CointegrationResult",
    "KalmanHedge",
    "KalmanHedgeState",
    "RegimeClassifier",
    "RegimeState",
    "engle_granger",
    "half_life",
]
