from __future__ import annotations

import pandas as pd

from trading_live_claude.backtest import BacktestEngine
from trading_live_claude.strategies import STRATEGIES


def test_backtest_runs_end_to_end(random_walk_df: pd.DataFrame) -> None:
    strat = STRATEGIES["ema_crossover"]()
    engine = BacktestEngine(starting_equity=100_000)
    result = engine.run(strat, random_walk_df, symbol="TEST")
    assert result.metrics.num_trades >= 0
    assert result.equity_curve.iloc[-1] > 0
    assert result.starting_equity == 100_000


def test_backtest_flags_short_window(trending_df: pd.DataFrame) -> None:
    short = trending_df.iloc[:100]
    strat = STRATEGIES["ema_crossover"]()
    result = BacktestEngine().run(strat, short, symbol="TEST")
    assert any("< 2 years" in w for w in result.warnings)


def test_summary_markdown_is_readable(random_walk_df: pd.DataFrame) -> None:
    strat = STRATEGIES["ema_crossover"]()
    result = BacktestEngine().run(strat, random_walk_df, symbol="TEST")
    md = result.summary_markdown()
    assert "Total return" in md
    assert "Sharpe" in md
    assert "Max drawdown" in md
