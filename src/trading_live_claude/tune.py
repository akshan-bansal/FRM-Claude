"""Auto-tune: backtest a strategy x symbol grid, score by Sharpe / |max DD|, pick winners.

The CLI command `trading tune` calls into this. Results are written to
`config/trading.yaml` so the autonomous daemon can pick them up on next restart.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Iterable

from .backtest import BacktestEngine
from .brokers.base import Broker
from .config import write_trading_yaml
from .data import CandleCache, MarketData
from .logging_setup import get_logger
from .strategies import STRATEGIES

log = get_logger(__name__)


@dataclass
class TuneResult:
    strategy: str
    symbol: str
    sharpe: float
    max_drawdown: float
    cagr: float
    win_rate: float
    num_trades: int
    score: float

    @classmethod
    def from_backtest(cls, strategy: str, symbol: str, m) -> "TuneResult":
        # Score = Sharpe / |max_drawdown|, with a small floor so flat-line cases don't divide-by-zero
        dd = abs(m.max_drawdown) if m.max_drawdown else 0.0
        score = m.sharpe / max(dd, 1e-4)
        return cls(
            strategy=strategy,
            symbol=symbol,
            sharpe=float(m.sharpe),
            max_drawdown=float(m.max_drawdown),
            cagr=float(m.cagr),
            win_rate=float(m.win_rate),
            num_trades=int(m.num_trades),
            score=float(score),
        )


# Curated universe: broad ETFs first (lower idiosyncratic risk), megacaps second.
DEFAULT_TUNE_UNIVERSE: tuple[str, ...] = (
    "XIC.TO", "VFV.TO", "XEF.TO", "XIU.TO", "ZAG.TO",
    "VOO", "SPY", "QQQ", "IWM", "VTI",
    "AAPL", "MSFT", "GOOGL", "NVDA", "AMZN",
    "RY.TO", "TD.TO", "ENB.TO",
)

DEFAULT_TUNE_STRATEGIES: tuple[str, ...] = (
    "bollinger", "rsi_meanrevert", "ema_crossover", "macd", "momentum_breakout",
)


def run_tune(
    broker: Broker,
    cache: CandleCache,
    *,
    symbols: Iterable[str] = DEFAULT_TUNE_UNIVERSE,
    strategies: Iterable[str] = DEFAULT_TUNE_STRATEGIES,
    years: float = 5.0,
    parallel: int = 4,
) -> list[TuneResult]:
    """Run every (strategy, symbol) combo and return ranked results."""
    market = MarketData(broker, cache=cache)
    engine = BacktestEngine()

    def _one(strategy_name: str, symbol: str) -> TuneResult | None:
        try:
            df = market.history(symbol=symbol, years=years, interval="1d")
            if df.empty or len(df) < 252:
                return None
            strat = STRATEGIES[strategy_name]()
            result = engine.run(strategy=strat, df=df, symbol=symbol, timeframe="1d")
            return TuneResult.from_backtest(strategy_name, symbol, result.metrics)
        except Exception as e:
            log.warning("tune.combo.failed", strategy=strategy_name, symbol=symbol, error=str(e))
            return None

    out: list[TuneResult] = []
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = [pool.submit(_one, s, sym) for s in strategies for sym in symbols]
        for fut in as_completed(futures):
            r = fut.result()
            if r is not None:
                out.append(r)
    out.sort(key=lambda r: r.score, reverse=True)
    return out


def pick_config(results: list[TuneResult], *, min_trades: int = 15, max_drawdown_cap: float = -0.20) -> dict | None:
    """From ranked results pick: best strategy, top 3 symbols for that strategy.

    Filters: require >= min_trades and max_drawdown >= max_drawdown_cap (i.e. not worse than -20%).
    Returns the trading.yaml update dict, or None if nothing passes filters.
    """
    eligible = [r for r in results if r.num_trades >= min_trades and r.max_drawdown >= max_drawdown_cap]
    if not eligible:
        return None
    winner = eligible[0]
    # Pick top-3 symbols that also use this strategy.
    same_strategy = [r for r in eligible if r.strategy == winner.strategy][:3]
    picked_symbols = [r.symbol for r in same_strategy] or [winner.symbol]

    update = {
        "default_strategy": winner.strategy,
        "default_symbols": ",".join(picked_symbols),
        "autonomous_strategy": winner.strategy,
        "autonomous_symbols": ",".join(picked_symbols),
        "last_tune": {
            "ran_at": datetime.now(UTC).isoformat(),
            "winner": asdict(winner),
            "picked_symbols": picked_symbols,
            "scoreboard": [asdict(r) for r in eligible[:20]],
        },
    }
    return update


def apply_tune(results: list[TuneResult], *, dry_run: bool = False) -> dict | None:
    update = pick_config(results)
    if update is None:
        return None
    if not dry_run:
        write_trading_yaml(update)
    return update
