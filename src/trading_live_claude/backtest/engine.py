"""Vectorized single-symbol backtest engine.

Implements article skill #1's recipe end-to-end:
  1. Confirm strategy params (caller's responsibility)
  2. Fetch historical OHLCV (caller passes the DataFrame)
  3. Compute indicators (strategy.generate_signals)
  4. Generate entry/exit signals (strategy.generate_signals)
  5. Run vectorized backtest -> equity curve + trade ledger
  6. Output Sharpe / max DD / win rate via metrics.compute_metrics
  7. Flag overfitting risk if window < 2 years
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from ..signals.generator import SignalSet
from ..strategies.base import Strategy, StrategyContext
from .costs import CostModel
from .metrics import Metrics, compute_metrics


@dataclass
class Trade:
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    pnl_pct: float
    bars_held: int


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    starting_equity: float
    ending_equity: float
    equity_curve: pd.Series
    returns: pd.Series
    positions: pd.Series
    trades: list[Trade]
    metrics: Metrics
    warnings: list[str] = field(default_factory=list)

    def summary_markdown(self) -> str:
        m = self.metrics
        rows = [
            f"# Backtest — {self.strategy} on {self.symbol}",
            "",
            f"- Starting equity: ${self.starting_equity:,.2f}",
            f"- Ending equity:   ${self.ending_equity:,.2f}",
            f"- Total return:    {m.total_return:.2%}",
            f"- CAGR:            {m.cagr:.2%}",
            f"- Sharpe:          {m.sharpe:.2f}",
            f"- Sortino:         {m.sortino:.2f}",
            f"- Max drawdown:    {m.max_drawdown:.2%}",
            f"- Win rate:        {m.win_rate:.2%}  (over {m.num_trades} trades)",
            f"- Avg trade:       {m.avg_trade_pct:.2%}",
            f"- Time in market:  {m.exposure_pct:.2%}",
        ]
        if self.warnings:
            rows += ["", "## Warnings", *(f"- {w}" for w in self.warnings)]
        return "\n".join(rows)


class BacktestEngine:
    def __init__(
        self,
        starting_equity: float = 100_000.0,
        commission_per_trade: float = 4.95,
        slippage_bps: float = 5.0,
        cost_model: CostModel | None = None,
    ) -> None:
        self.starting_equity = starting_equity
        self.commission_per_trade = commission_per_trade
        self.slippage_bps = slippage_bps
        # Default reproduces the original behaviour (slippage only). Pass a CostModel to run a
        # backtest net of commission + spread (see CostModel.from_price for a realistic per-name one).
        self.cost_model = cost_model or CostModel.legacy(slippage_bps)

    def run(
        self,
        strategy: Strategy,
        df: pd.DataFrame,
        symbol: str,
        timeframe: str = "1d",
    ) -> BacktestResult:
        if "close" not in df.columns:
            raise ValueError("DataFrame must contain a 'close' column.")
        ctx = StrategyContext(symbol=symbol, timeframe=timeframe)
        signals = strategy.generate_signals(df, ctx)
        position = SignalSet(signals).to_positions(
            atr_stop_mult=strategy.stop_atr_mult,
            trail_atr_mult=strategy.trail_atr_mult,
            time_stop_bars=strategy.time_stop_bars,
        )

        per_side = self.cost_model.per_side_frac()
        bar_ret = signals["close"].pct_change().fillna(0.0)
        # apply cost (commission + slippage + half-spread) on each position transition
        transition = position.diff().abs().fillna(0)
        cost = transition * per_side
        strat_ret = position.shift(1).fillna(0) * bar_ret - cost
        equity = self.starting_equity * (1.0 + strat_ret).cumprod()

        # trade ledger
        trades = self._build_trade_ledger(signals, position)
        trade_pnls = [t.pnl_pct for t in trades]

        metrics = compute_metrics(equity, strat_ret, trade_pnls, position)

        warnings: list[str] = []
        years_in_sample = len(signals) / 252.0
        if years_in_sample < 2.0:
            warnings.append(
                f"Backtest window < 2 years ({years_in_sample:.2f}y). "
                "Treat results as preliminary — high overfitting risk."
            )
        if metrics.num_trades < 30:
            warnings.append(
                f"Only {metrics.num_trades} trades — too few for statistical significance."
            )
        if metrics.sharpe > 3.0:
            warnings.append(
                f"Sharpe {metrics.sharpe:.2f} is suspiciously high; check for lookahead, "
                "survivorship bias, or unrealistic fills."
            )

        return BacktestResult(
            symbol=symbol,
            strategy=strategy.name,
            starting_equity=self.starting_equity,
            ending_equity=float(equity.iloc[-1]) if not equity.empty else self.starting_equity,
            equity_curve=equity,
            returns=strat_ret,
            positions=position,
            trades=trades,
            metrics=metrics,
            warnings=warnings,
        )

    def _build_trade_ledger(self, signals: pd.DataFrame, position: pd.Series) -> list[Trade]:
        """Round-trips from the position track. Handles longs (+1) and shorts (-1),
        including a direct long↔short flip, which closes one trade and opens the next."""
        trades: list[Trade] = []
        side = 0  # current position sign
        entry_idx = -1
        entry_price = 0.0

        def close(i: int) -> None:
            nonlocal side
            exit_price = float(signals["close"].iloc[i])
            # long: exit/entry - 1;  short: inverse. ``side`` carries the sign.
            pnl_pct = (exit_price / entry_price - 1.0) * side if entry_price > 0 else 0.0
            trades.append(
                Trade(
                    entry_time=pd.Timestamp(signals["time"].iloc[entry_idx]).to_pydatetime() if "time" in signals.columns else datetime.now(),
                    exit_time=pd.Timestamp(signals["time"].iloc[i]).to_pydatetime() if "time" in signals.columns else datetime.now(),
                    entry_price=entry_price,
                    exit_price=exit_price,
                    pnl_pct=pnl_pct,
                    bars_held=i - entry_idx,
                )
            )
            side = 0

        for i in range(len(position)):
            pos = int(position.iloc[i])
            if side != 0 and pos != side:
                close(i)
            if side == 0 and pos != 0:
                side = pos
                entry_idx = i
                entry_price = float(signals["close"].iloc[i])
        return trades
