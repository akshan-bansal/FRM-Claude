"""External-platform integrations that sit alongside the Questrade core.

Currently: a QuantConnect REST API v2 client for driving cloud projects,
compiles, and backtests from this repo. Kept separate from ``brokers`` because
QuantConnect is not a broker in this framework's sense — it's a research/backtest
cloud, not an execution venue for the Router.
"""
from __future__ import annotations

from .qc_library import (
    QcStrategy,
    QcStrategyAnalysis,
    analyze_library,
    analyze_source,
    categorize,
    categorize_source,
    detect_indicators,
    list_library,
    pull_algorithm,
)
from .quantconnect import QuantConnectClient, QuantConnectError

__all__ = [
    "QcStrategy",
    "QcStrategyAnalysis",
    "QuantConnectClient",
    "QuantConnectError",
    "analyze_library",
    "analyze_source",
    "categorize",
    "categorize_source",
    "detect_indicators",
    "list_library",
    "pull_algorithm",
]
