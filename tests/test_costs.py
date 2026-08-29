from __future__ import annotations

import numpy as np
import pandas as pd

from trading_live_claude.backtest import BacktestEngine
from trading_live_claude.backtest.costs import CostModel
from trading_live_claude.strategies import STRATEGIES


def test_per_side_frac_combines_bps_and_fixed() -> None:
    cm = CostModel(commission_bps=1.0, slippage_bps=5.0, half_spread_bps=2.0,
                   commission_per_trade=5.0, notional_per_trade=100_000.0)
    # (1+5+2) bps = 8 bps = 0.0008, plus 5/100000 = 0.00005
    assert abs(cm.per_side_frac() - (0.0008 + 0.00005)) < 1e-12


def test_presets() -> None:
    assert CostModel.frictionless().per_side_frac() == 0.0
    assert CostModel.legacy(5.0).per_side_frac() == 0.0005          # slippage only
    etf = CostModel.from_price(700.0, is_etf=True)
    eq = CostModel.from_price(8.0, is_etf=False)
    assert etf.commission_bps == 0.0 and eq.commission_bps > 0.0    # ETFs commission-free
    assert eq.half_spread_bps > etf.half_spread_bps                 # cheap stock -> wider relative spread


def _trending_df(n: int = 300, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.015, n)))
    return pd.DataFrame({"time": pd.date_range("2022-01-01", periods=n, freq="B", tz="UTC"),
                         "open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1e6})


def test_costs_reduce_return_monotonically() -> None:
    df = _trending_df()
    strat = STRATEGIES["bollinger"]()
    gross = BacktestEngine(cost_model=CostModel.frictionless()).run(strat, df, symbol="X").metrics.total_return
    cheap = BacktestEngine(cost_model=CostModel(slippage_bps=5)).run(strat, df, symbol="X").metrics.total_return
    dear = BacktestEngine(cost_model=CostModel(slippage_bps=5, commission_bps=5, half_spread_bps=10)).run(strat, df, symbol="X").metrics.total_return
    assert gross >= cheap >= dear                                  # more cost -> less return


def test_default_engine_is_backward_compatible() -> None:
    """Default engine (no cost_model) must equal the legacy slippage-only behaviour bit-for-bit."""
    df = _trending_df(seed=3)
    strat = STRATEGIES["rsi_meanrevert"]()
    default = BacktestEngine().run(strat, df, symbol="X").metrics.total_return
    legacy = BacktestEngine(cost_model=CostModel.legacy(5.0)).run(strat, df, symbol="X").metrics.total_return
    assert default == legacy
