"""Backtest metrics: returns, Sharpe, max DD, win rate (article skill #1 recipe step 5)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Metrics:
    total_return: float
    cagr: float
    sharpe: float
    sortino: float
    max_drawdown: float
    win_rate: float
    num_trades: int
    avg_trade_pct: float
    exposure_pct: float


def compute_metrics(
    equity: pd.Series,
    returns: pd.Series,
    trade_pnls: list[float],
    position: pd.Series,
    bars_per_year: int = 252,
) -> Metrics:
    if equity.empty:
        return Metrics(0, 0, 0, 0, 0, 0, 0, 0, 0)

    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    n_bars = max(len(equity), 1)
    years = n_bars / bars_per_year
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1.0 / max(years, 1e-9)) - 1.0)

    r = returns.dropna()
    if r.std(ddof=0) > 0:
        sharpe = float(np.sqrt(bars_per_year) * r.mean() / r.std(ddof=0))
    else:
        sharpe = 0.0

    downside = r[r < 0]
    if downside.std(ddof=0) > 0:
        sortino = float(np.sqrt(bars_per_year) * r.mean() / downside.std(ddof=0))
    else:
        sortino = 0.0

    roll_max = equity.cummax()
    drawdowns = equity / roll_max - 1.0
    max_dd = float(drawdowns.min()) if not drawdowns.empty else 0.0

    wins = [p for p in trade_pnls if p > 0]
    win_rate = float(len(wins) / len(trade_pnls)) if trade_pnls else 0.0
    avg_trade = float(np.mean(trade_pnls)) if trade_pnls else 0.0
    exposure = float(position.astype(bool).mean()) if not position.empty else 0.0

    return Metrics(
        total_return=total_return,
        cagr=cagr,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_dd,
        win_rate=win_rate,
        num_trades=len(trade_pnls),
        avg_trade_pct=avg_trade,
        exposure_pct=exposure,
    )
