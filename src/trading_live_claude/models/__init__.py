"""Quantitative primitives shared by strategies: Kalman filtering, cointegration tests,
and time-series (ARIMA / GARCH) forecasting. These are pure model code — no I/O, no broker,
no lookahead — so they can be unit-tested in isolation and composed by Strategy subclasses."""
from __future__ import annotations

from .cointegration import CointegrationResult, engle_granger, half_life
from .cross_sectional import CrossSectionalRanker, RankerResult, build_panel
from .kalman import KalmanHedge, KalmanHedgeState
from .pruning import PruneResult, forward_select
from .regime import RegimeClassifier, RegimeState
from .selection import FEATURES, SelectionModel

__all__ = [
    "FEATURES",
    "CointegrationResult",
    "CrossSectionalRanker",
    "KalmanHedge",
    "KalmanHedgeState",
    "PruneResult",
    "RankerResult",
    "RegimeClassifier",
    "RegimeState",
    "SelectionModel",
    "build_panel",
    "engle_granger",
    "forward_select",
    "half_life",
]
