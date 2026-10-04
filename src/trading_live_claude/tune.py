"""Auto-tune: backtest a strategy x symbol grid, score by Sharpe / |max DD|, pick winners.

The CLI command `trading tune` calls into this. Results are written to
`config/trading.yaml` so the autonomous daemon can pick them up on next restart.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from .analysis.classification import confusion
from .analysis.labeling import forward_return, label_events
from .analysis.lens_audit import MAX_EVENTS, LensResult, future_by_group, reliability_by_group
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

DEFAULT_LABEL_HORIZON = 10  # bars; the forward window run_tune labels (and the lens audit reads)


# --------------------------------------------------------------------------- lens audit (advisory)
#
# An optional second opinion printed beside the scoreboard (``trading tune --lens-audit``). It is a
# ruler, not a gate: it never reorders ``results``, never feeds ``pick_config`` / ``apply_tune`` and
# adds no field to ``TuneResult`` (``asdict`` of that is what gets persisted to trading.yaml).

ENTRY_FEATURES: tuple[str, ...] = ("ret_5", "ret_20", "ret_60", "vol_20")
# Daily bars: 10 trading days can span ~14 calendar days. 1.6 calendar days per bar is a
# conservative upper bound, so a label window is never treated as closed before it really is.
AUDIT_DAYS_PER_BAR = 1.6

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class CandidateEvents:
    """Entry-bar observations for one (strategy, symbol), kept only for the optional lens audit."""

    strategy: str
    symbol: str
    day: FloatArray  # calendar day number of each entry bar
    features: FloatArray  # n x len(ENTRY_FEATURES), all known at the bar's close
    outcome: FloatArray  # forward return over the label horizon


@dataclass(frozen=True)
class LensVerdicts:
    """What the audit says about one scoreboard row. ``None`` means too little data to say."""

    reliability: LensResult | None  # read from the same strategy's entries on other symbols
    future: LensResult | None  # read from this symbol's own matured history
    n_events: int
    thinned_by: int = 1


def entry_events(
    strategy: str, symbol: str, df: pd.DataFrame, entry: pd.Series, *, horizon: int
) -> CandidateEvents | None:
    """Entry bars with backward-looking features and the realized forward return.

    Features use only data up to and including the bar (trailing returns, trailing volatility),
    the same anchor ``label_events`` uses, so nothing here looks ahead. Bars whose forward window
    runs off the end, or whose trailing window is incomplete, are dropped, never zero-filled.
    """
    if "time" not in df.columns:
        return None  # cannot align events across symbols without dates
    close = df["close"].astype(float)
    feats = pd.DataFrame({
        "ret_5": close.pct_change(5),
        "ret_20": close.pct_change(20),
        "ret_60": close.pct_change(60),
        "vol_20": close.pct_change().rolling(20).std(),
    })
    fwd = forward_return(close, horizon)
    fired = entry.astype(float).clip(0, 1).round().astype(bool)
    ok = fired & feats.notna().all(axis=1) & fwd.notna()
    if not bool(ok.any()):
        return None
    days = (pd.to_datetime(df["time"], utc=True) - pd.Timestamp("1970-01-01", tz="UTC")).dt.days
    return CandidateEvents(
        strategy=strategy,
        symbol=symbol,
        day=days[ok].to_numpy(dtype=float),
        features=feats[ok].to_numpy(dtype=float),
        outcome=fwd[ok].to_numpy(dtype=float),
    )


def _thin(events: list[CandidateEvents], limit: int) -> tuple[list[CandidateEvents], int]:
    """Keep every k-th event per symbol until the total fits the lens module's dense-matrix limit."""
    stride = 1
    while sum(-(-len(e.outcome) // stride) for e in events) > limit:
        stride += 1
    if stride == 1:
        return events, 1
    return [
        replace(e, day=e.day[::stride], features=e.features[::stride], outcome=e.outcome[::stride])
        for e in events
    ], stride


def audit_candidates(
    events: Iterable[CandidateEvents],
    *,
    label_horizon: int = DEFAULT_LABEL_HORIZON,
    n_perm: int = 500,
    seed: int = 7,
) -> dict[tuple[str, str], LensVerdicts]:
    """Lens verdicts per (strategy, symbol), one attention matrix per strategy.

    Events for a strategy are pooled across its symbols; each symbol is then scored separately
    (read only from the other symbols for reliability, only from its own matured past for future).
    A strategy or symbol with too little data gets ``None`` for that lens, never a made-up verdict.
    """
    by_strategy: dict[str, list[CandidateEvents]] = {}
    for e in events:
        by_strategy.setdefault(e.strategy, []).append(e)
    horizon_days = math.ceil(label_horizon * AUDIT_DAYS_PER_BAR)

    out: dict[tuple[str, str], LensVerdicts] = {}
    for strategy in sorted(by_strategy):
        # sorted by symbol so the answer does not depend on which backtest thread finished first
        evs, stride = _thin(sorted(by_strategy[strategy], key=lambda e: e.symbol), MAX_EVENTS)
        x = np.vstack([e.features for e in evs])
        y = np.concatenate([e.outcome for e in evs])
        t = np.concatenate([e.day for e in evs])
        g = np.concatenate([np.full(len(e.outcome), e.symbol) for e in evs])
        rel: dict[object, LensResult] = {}
        fut: dict[object, LensResult] = {}
        try:
            rel = reliability_by_group(x, y, g, time=t, label_horizon=horizon_days,
                                       n_perm=n_perm, seed=seed)
        except ValueError as exc:
            log.info("tune.lens_audit.reliability_skipped", strategy=strategy, reason=str(exc))
        try:
            fut = future_by_group(x, y, g, time=t, label_horizon=horizon_days,
                                  n_perm=n_perm, seed=seed)
        except ValueError as exc:
            log.info("tune.lens_audit.future_skipped", strategy=strategy, reason=str(exc))
        for e in evs:
            out[(strategy, e.symbol)] = LensVerdicts(
                rel.get(e.symbol), fut.get(e.symbol), len(e.outcome), stride)
    return out


def lens_cell(r: LensResult | None) -> str:
    """One table cell: a yes/no verdict with the numbers behind it, or n/a."""
    if r is None:
        return "n/a"
    return f"{'yes' if r.supported else 'no'} (rho {r.spearman:+.2f}, p {r.p_value:.2f})"


def run_tune(
    broker: Broker,
    cache: CandleCache,
    *,
    symbols: Iterable[str] = DEFAULT_TUNE_UNIVERSE,
    strategies: Iterable[str] = DEFAULT_TUNE_STRATEGIES,
    years: float = 5.0,
    parallel: int = 4,
    objective: str = DEFAULT_OBJECTIVE,
    label_horizon: int = DEFAULT_LABEL_HORIZON,
    label_up_threshold: float = 0.03,
    audit_sink: list[CandidateEvents] | None = None,
) -> list[TuneResult]:
    """Run every (strategy, symbol) combo and return ranked results.

    Each combo is scored on ``objective`` (a name in ``scoring.objective``) and
    carries its signal-quality precision/recall computed against forward-return
    labels, so the scoreboard shows both stages' health next to P&L.

    ``audit_sink``, when given, collects each successful combo's entry-bar events for the optional
    lens audit. It is write-only from here: results, ranking and scores are identical with or
    without it.
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
            row = TuneResult.from_backtest(
                strategy_name,
                symbol,
                result.metrics,
                precision=rep.precision,
                recall=rep.recall,
                objective=objective,
            )
            if audit_sink is not None:
                # Own handler: a fault in the optional audit must never cost a candidate its row.
                try:
                    ev = entry_events(strategy_name, symbol, df, signals["entry"], horizon=label_horizon)
                except Exception as audit_exc:
                    log.warning("tune.lens_audit.events_failed", strategy=strategy_name,
                                symbol=symbol, error=str(audit_exc))
                else:
                    if ev is not None:
                        audit_sink.append(ev)  # list.append is atomic under the GIL
            return row
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
