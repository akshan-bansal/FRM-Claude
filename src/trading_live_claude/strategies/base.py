"""Strategy abstract base class.

Implementers override ``generate_signals(df)`` to add at least the columns:
  * ``entry``  -> 0/1  (long entry trigger on next-bar open)
  * ``exit``   -> 0/1  (long exit trigger on next-bar open)
  * optional ``size_hint`` -> float in [0, 1], conviction weight
  * optional ``atr``       -> float; used by the position sizer

Strategies must NOT execute orders. They produce a DataFrame; the router does
the executing. This is the same contract used in backtest, paper, and live.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class StrategyContext:
    """Per-symbol context passed to ``generate_signals``."""

    symbol: str
    timeframe: str = "1d"
    params: dict = field(default_factory=dict)


class Strategy(ABC):
    name: str = "abstract"
    description: str = ""

    def __init__(self, **params: object) -> None:
        self.params = params

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        """Return the input DataFrame augmented with at minimum: entry, exit."""

    def required_history_bars(self) -> int:
        """Minimum bars needed to compute signals (warm-up period for live loop)."""
        return 250
