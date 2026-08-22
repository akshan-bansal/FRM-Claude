"""Signal matrix — the strategy x symbol dashboard of {recall, precision, risk}.

This is the measurement dashboard for the whole two-stage effort. For each
(strategy, symbol) it reports:
  * ``recall``    — signal-stage health (did we catch the real moves?)
  * ``precision`` — scoring-stage health (were the fired signals real?)
  * ``max_drawdown`` — the risk axis, straight from the backtest metrics

It is a **pure** function of the OHLCV frames handed to it, so it runs and is
tested entirely on synthetic data — no broker or Questrade token required. A live
caller (``tune``) supplies real frames fetched from Questrade; a test supplies
synthetic ones. Same code path.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd

from ..backtest.engine import BacktestEngine
from ..risk.tail import expected_shortfall
from ..signals.generator import candidate_strength
from ..strategies import STRATEGIES
from ..strategies.base import StrategyContext
from .classification import confusion
from .fidelity import fidelity
from .labeling import forward_return, label_events
from .roc import roc_auc


@dataclass(frozen=True)
class MatrixCell:
    strategy: str
    symbol: str
    recall: float          # sensitivity: fraction of real +X% moves the signal caught
    specificity: float     # fraction of non-moves the signal correctly avoided
    precision: float
    max_drawdown: float    # the risk axis
    num_trades: int
    support: int
    roc_auc: float = 0.5   # threshold-independent ranking quality (0.5 = chance)
    fidelity: float = 0.0  # 5th axis: temporal stability of the signal↔return edge
    cvar: float = 0.0      # tail risk: Expected Shortfall (mean loss in the worst 5%)

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "recall": round(self.recall, 4),
            "specificity": round(self.specificity, 4),
            "precision": round(self.precision, 4),
            "roc_auc": round(self.roc_auc, 4),
            "fidelity": round(self.fidelity, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "cvar": round(self.cvar, 4),
            "num_trades": self.num_trades,
            "support": self.support,
        }


def build_signal_matrix(
    frames: Mapping[str, pd.DataFrame],
    *,
    strategies: list[str] | None = None,
    horizon: int = 10,
    up_threshold: float = 0.03,
    min_bars: int = 252,
) -> list[MatrixCell]:
    """Compute a MatrixCell for every (strategy, symbol) over the supplied frames.

    ``strategies`` defaults to every registered single-symbol strategy (``pairs``
    excluded — it needs two symbols). Frames shorter than ``min_bars`` are skipped.
    Recall/precision use forward-return labels (``horizon``, ``up_threshold``);
    risk (max drawdown) comes from the existing backtest engine.
    """
    strat_names = strategies if strategies is not None else [s for s in STRATEGIES if s != "pairs"]
    engine = BacktestEngine()
    cells: list[MatrixCell] = []

    for symbol, df in frames.items():
        if df is None or df.empty or len(df) < min_bars:
            continue
        labels = label_events(df, horizon=horizon, up_threshold=up_threshold)
        fwd = forward_return(df["close"], horizon)
        for name in strat_names:
            strat = STRATEGIES[name]()
            signals = strat.generate_signals(df, StrategyContext(symbol=symbol))
            strength = candidate_strength(signals)
            rep = confusion(signals["entry"], labels)
            auc = roc_auc(strength, labels)
            fid = fidelity(strength, fwd)
            result = engine.run(strat, df, symbol=symbol)
            metrics = result.metrics
            es = expected_shortfall(result.returns)
            cells.append(
                MatrixCell(
                    strategy=name,
                    symbol=symbol,
                    recall=rep.recall,
                    specificity=rep.specificity,
                    precision=rep.precision,
                    max_drawdown=metrics.max_drawdown,
                    num_trades=metrics.num_trades,
                    support=rep.support,
                    roc_auc=auc,
                    fidelity=fid,
                    cvar=es,
                )
            )
    return cells


def render_matrix_markdown(cells: list[MatrixCell]) -> str:
    """Render cells as a markdown table sorted by sensitivity x specificity, desc."""
    if not cells:
        return "# Signal matrix\n\n_No cells — no frames met the minimum bar count._\n"

    ordered = sorted(cells, key=lambda c: (c.recall * c.specificity, -abs(c.max_drawdown)), reverse=True)
    header = (
        "# Signal matrix — sensitivity x specificity x risk\n\n"
        "Sensitivity (recall) = fraction of real +move events the signal caught. "
        "Specificity = fraction of non-moves it correctly avoided. Precision = of the "
        "signals fired, how many were real. Risk = max drawdown. "
        "Sorted by sensitivity x specificity, then shallowest risk.\n\n"
        "| Strategy | Symbol | Sensitivity | Specificity | Precision | ROC AUC | Fidelity | Max DD | CVaR |\n"
        "|---|---|---:|---:|---:|---:|---:|---:|---:|\n"
    )
    rows = [
        f"| {c.strategy} | {c.symbol} | {c.recall:.2%} | {c.specificity:.2%} | {c.precision:.2%} "
        f"| {c.roc_auc:.3f} | {c.fidelity:+.3f} | {c.max_drawdown:.2%} | {c.cvar:.2%} |"
        for c in ordered
    ]
    return header + "\n".join(rows) + "\n"
