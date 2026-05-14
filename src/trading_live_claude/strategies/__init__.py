from __future__ import annotations

from .base import Strategy, StrategyContext
from .examples.bollinger import BollingerMeanRevert
from .examples.ema_crossover import EmaCrossover
from .examples.macd import MacdSignalCross
from .examples.momentum_breakout import DonchianBreakout
from .examples.pairs import PairsZScore
from .examples.rsi_meanrevert import RsiMeanRevert

STRATEGIES: dict[str, type[Strategy]] = {
    "ema_crossover": EmaCrossover,
    "rsi_meanrevert": RsiMeanRevert,
    "macd": MacdSignalCross,
    "bollinger": BollingerMeanRevert,
    "momentum_breakout": DonchianBreakout,
    "pairs": PairsZScore,
}

__all__ = ["STRATEGIES", "Strategy", "StrategyContext"]
