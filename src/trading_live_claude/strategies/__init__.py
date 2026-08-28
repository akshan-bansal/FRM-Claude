from __future__ import annotations

from .base import Strategy, StrategyContext
from .candlestick import CANDLE_STRATEGIES, CandlestickStrategy
from .composite import CompositeStrategy, DefaultComposite
from .examples.bollinger import BollingerMeanRevert
from .examples.ema_crossover import EmaCrossover
from .examples.macd import MacdSignalCross
from .examples.mean_reversion import BbRsiCombo, Rsi2Connors, ZScoreOU
from .examples.momentum import DualMa, High52wBreakout, TsMomentum
from .examples.momentum_breakout import DonchianBreakout
from .examples.pairs import KalmanPairs, PairsZScore
from .examples.rsi_meanrevert import RsiMeanRevert
from .examples.seasonality import DayOfWeek, MonthOfYear, TurnOfMonth
from .examples.volatility import AtrChannel, BbWidthSqueeze, VolTarget
from .overlay import CONFIRM_STRATEGIES, ConfirmOverlay
from .valuation import VALUATION_STRATEGIES, ValuationStrategy

STRATEGIES: dict[str, type[Strategy]] = {
    # core (original)
    "ema_crossover": EmaCrossover,
    "rsi_meanrevert": RsiMeanRevert,
    "macd": MacdSignalCross,
    "bollinger": BollingerMeanRevert,
    "momentum_breakout": DonchianBreakout,
    "pairs": PairsZScore,
    "kalman_pairs": KalmanPairs,
    "composite": DefaultComposite,
    # mean-reversion (advanced)
    "rsi2_connors": Rsi2Connors,
    "zscore_ou": ZScoreOU,
    "bb_rsi_combo": BbRsiCombo,
    # momentum (advanced)
    "ts_momentum": TsMomentum,
    "dual_ma": DualMa,
    "high_52w_breakout": High52wBreakout,
    # volatility
    "atr_channel": AtrChannel,
    "bbwidth_squeeze": BbWidthSqueeze,
    "vol_target": VolTarget,
    # seasonality
    "turn_of_month": TurnOfMonth,
    "day_of_week": DayOfWeek,
    "month_of_year": MonthOfYear,
}

# Candlestick-pattern strategies (one per bullish pattern) join the pipeline.
STRATEGIES.update(CANDLE_STRATEGIES)
# Candlestick confirmation overlays over the entire mean-reversion set (precision stage).
STRATEGIES.update(CONFIRM_STRATEGIES)
# Valuation strategies: mean-reversion run on equity-valuation ratios + price overlaps.
STRATEGIES.update(VALUATION_STRATEGIES)

__all__ = [
    "STRATEGIES",
    "CandlestickStrategy",
    "CompositeStrategy",
    "ConfirmOverlay",
    "DefaultComposite",
    "Strategy",
    "StrategyContext",
    "ValuationStrategy",
]
