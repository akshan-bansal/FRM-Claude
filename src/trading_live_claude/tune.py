"""Auto-tune: backtest a strategy x symbol grid, score by Sharpe / |max DD|, pick winners.

The CLI command `trading tune` calls into this. Results are written to
`config/trading.yaml` so the autonomous daemon can pick them up on next restart.
"""
from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from .analysis.classification import confusion
from .analysis.labeling import label_events
from .backtest import BacktestEngine
from .backtest.metrics import Metrics
from .brokers.base import Broker
from .config import write_trading_yaml
from .data import CandleCache, MarketData
from .logging_setup import get_logger
from .scoring.objective import ObjectiveAdapter, ObjectiveInput
from .analysis.calibration import calibrate_for
from .strategies import STRATEGIES
from .strategies.base import StrategyContext

log = get_logger(__name__)

# Default tuning objective: Sortino / |max drawdown| — downside-risk-adjusted return.
# Swap to "sharpe_over_dd", "precision", "f_beta", … via the scoring.objective registry.
DEFAULT_OBJECTIVE = "sortino_over_dd"


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
    sortino: float = 0.0
    precision: float | None = None
    recall: float | None = None

    @classmethod
    def from_backtest(
        cls,
        strategy: str,
        symbol: str,
        m: Metrics,
        *,
        precision: float | None = None,
        recall: float | None = None,
        objective: str = DEFAULT_OBJECTIVE,
    ) -> TuneResult:
        # Score comes from the swappable objective adapter, so the optimization
        # target is a config string rather than a hardcoded ratio (default Sortino/DD).
        oi = ObjectiveInput(
            sharpe=float(m.sharpe),
            max_drawdown=float(m.max_drawdown),
            sortino=float(m.sortino),
            cagr=float(m.cagr),
            win_rate=float(m.win_rate),
            num_trades=int(m.num_trades),
            precision=precision,
            recall=recall,
        )
        score = ObjectiveAdapter.from_name(objective).score(oi)
        return cls(
            strategy=strategy,
            symbol=symbol,
            sharpe=float(m.sharpe),
            max_drawdown=float(m.max_drawdown),
            cagr=float(m.cagr),
            win_rate=float(m.win_rate),
            num_trades=int(m.num_trades),
            score=float(score),
            sortino=float(m.sortino),
            precision=precision,
            recall=recall,
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
    objective: str = DEFAULT_OBJECTIVE,
    label_horizon: int = 10,
    label_up_threshold: float = 0.03,
) -> list[TuneResult]:
    """Run every (strategy, symbol) combo and return ranked results.

    Each combo is scored on ``objective`` (a name in ``scoring.objective``) and
    carries its signal-quality precision/recall computed against forward-return
    labels, so the scoreboard shows both stages' health next to P&L.
    """
    market = MarketData(broker, cache=cache)
    engine = BacktestEngine()

    def _one(strategy_name: str, symbol: str) -> TuneResult | None:
        try:
            df = market.history(symbol=symbol, years=years, interval="1d")
            if df.empty or len(df) < 252:
                return None
            # Asset-class calibrated instance: Bollinger bands widen for crypto, tighten
            # for FX; windows scale with the symbol's mean-reversion half-life. Strategies
            # without a calibrator (kalman_pairs, arima_garch, …) fall through to defaults.
            if strategy_name not in STRATEGIES:
                return None
            strat = calibrate_for(strategy_name, symbol)
            signals = strat.generate_signals(df, StrategyContext(symbol=symbol))
            labels = label_events(df, horizon=label_horizon, up_threshold=label_up_threshold)
            rep = confusion(signals["entry"], labels)
            result = engine.run(strategy=strat, df=df, symbol=symbol, timeframe="1d")
            return TuneResult.from_backtest(
                strategy_name,
                symbol,
                result.metrics,
                precision=rep.precision,
                recall=rep.recall,
                objective=objective,
            )
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


def best_strategy_per_symbol(results: list[TuneResult], *, min_trades: int = 5) -> dict[str, str]:
    """Map each symbol to its highest-scoring strategy (for per-symbol monitoring).

    Only combos with at least ``min_trades`` are eligible, so a symbol isn't assigned
    a strategy that barely traded. Symbols with no eligible combo are omitted.
    """
    best: dict[str, TuneResult] = {}
    for r in results:
        if r.num_trades < min_trades:
            continue
        cur = best.get(r.symbol)
        if cur is None or r.score > cur.score:
            best[r.symbol] = r
    return {symbol: r.strategy for symbol, r in best.items()}


def pick_config(results: list[TuneResult], *, min_trades: int = 15, max_drawdown_cap: float = -0.20) -> dict[str, object] | None:
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

    update: dict[str, object] = {
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


def apply_tune(results: list[TuneResult], *, dry_run: bool = False) -> dict[str, object] | None:
    update = pick_config(results)
    if update is None:
        return None
    if not dry_run:
        write_trading_yaml(update)
    return update
