"""Native parameter optimization — grid-search a strategy's tunable knobs.

Complements the QuantConnect cloud optimizer (which needs a paid node): this runs
free over Questrade data. For every combination in a parameter grid it instantiates
the strategy, backtests it, and scores it through the swappable ``ObjectiveAdapter``
(default Sortino/|maxDD|). Returns the combos ranked best-first.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

from .analysis.classification import confusion
from .analysis.labeling import label_events
from .backtest import BacktestEngine
from .brokers.base import Broker
from .data import CandleCache, MarketData
from .logging_setup import get_logger
from .scoring.objective import ObjectiveAdapter, ObjectiveInput
from .strategies import STRATEGIES
from .strategies.base import StrategyContext

log = get_logger(__name__)

# Sensible default sweep per strategy (parameter -> candidate values).
PARAM_GRIDS: dict[str, dict[str, list[Any]]] = {
    "bollinger": {"window": [10, 15, 20, 30], "n_std": [1.5, 2.0, 2.5, 3.0]},
    "rsi_meanrevert": {"window": [7, 14, 21], "oversold": [20, 25, 30, 35]},
    "ema_crossover": {"fast": [10, 20, 50], "slow": [50, 100, 200]},
    "macd": {"fast": [8, 12, 16], "slow": [21, 26, 34]},
    "momentum_breakout": {"entry_window": [20, 40, 55], "exit_window": [10, 20, 30]},
    "atr_channel": {"ema_window": [10, 20, 30], "k": [1.5, 2.0, 2.5, 3.0]},
    "ts_momentum": {"lookback": [63, 126, 189], "threshold": [0.0, 0.02, 0.05]},
    "dual_ma": {"fast": [20, 50], "slow": [100, 150, 200]},
    "high_52w_breakout": {"high_window": [126, 189, 252], "exit_window": [42, 63, 84]},
}


@dataclass(frozen=True)
class ParamResult:
    params: dict[str, Any]
    score: float
    sortino: float
    sharpe: float
    max_drawdown: float
    num_trades: int
    precision: float
    recall: float


def _valid(params: dict[str, Any]) -> bool:
    """Skip degenerate combos (e.g. fast >= slow for a crossover)."""
    if "fast" in params and "slow" in params and params["fast"] >= params["slow"]:
        return False
    if "entry_window" in params and "exit_window" in params:
        return bool(params["exit_window"] <= params["entry_window"])
    return True


def optimize_parameters(
    broker: Broker,
    cache: CandleCache,
    *,
    strategy: str,
    symbol: str,
    param_grid: dict[str, list[Any]] | None = None,
    objective: str = "sortino_over_dd",
    years: float = 5.0,
    min_trades: int = 5,
    horizon: int = 10,
    up_threshold: float = 0.03,
) -> list[ParamResult]:
    """Grid-search ``strategy`` on ``symbol``; return combos ranked by ``objective`` desc."""
    if strategy not in STRATEGIES:
        raise KeyError(f"Unknown strategy {strategy!r}")
    grid = param_grid or PARAM_GRIDS.get(strategy)
    if not grid:
        raise ValueError(f"No parameter grid for {strategy!r}; pass param_grid explicitly.")

    market = MarketData(broker, cache=cache)
    engine = BacktestEngine()
    adapter = ObjectiveAdapter.from_name(objective)
    df = market.history(symbol=symbol, years=years, interval="1d")
    if df.empty or len(df) < 252:
        return []
    labels = label_events(df, horizon=horizon, up_threshold=up_threshold)
    ctx = StrategyContext(symbol=symbol)

    names = list(grid)
    results: list[ParamResult] = []
    for combo in itertools.product(*(grid[n] for n in names)):
        params = dict(zip(names, combo, strict=True))
        if not _valid(params):
            continue
        try:
            strat = STRATEGIES[strategy](**params)
            signals = strat.generate_signals(df, ctx)
            rep = confusion(signals["entry"], labels)
            m = engine.run(strat, df, symbol=symbol).metrics
        except Exception as e:  # a bad combo shouldn't abort the sweep
            log.warning("optimize.combo.failed", strategy=strategy, params=params, error=str(e))
            continue
        if m.num_trades < min_trades:
            continue
        score = adapter.score(
            ObjectiveInput(
                sharpe=m.sharpe, max_drawdown=m.max_drawdown, sortino=m.sortino,
                cagr=m.cagr, win_rate=m.win_rate, num_trades=m.num_trades,
                precision=rep.precision, recall=rep.recall,
            )
        )
        results.append(
            ParamResult(
                params=params, score=float(score), sortino=float(m.sortino),
                sharpe=float(m.sharpe), max_drawdown=float(m.max_drawdown),
                num_trades=int(m.num_trades), precision=rep.precision, recall=rep.recall,
            )
        )
    results.sort(key=lambda r: r.score, reverse=True)
    return results
