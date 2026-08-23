from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_live_claude import optimize as opt
from trading_live_claude.optimize import PARAM_GRIDS, _valid, optimize_parameters


def _synth(n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.015, n)))
    return pd.DataFrame(
        {
            "time": pd.date_range("2022-01-01", periods=n, freq="B", tz="UTC"),
            "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": [1e6] * n,
        }
    )


def test_valid_filters_degenerate_combos() -> None:
    assert not _valid({"fast": 50, "slow": 50})
    assert _valid({"fast": 20, "slow": 100})
    assert not _valid({"entry_window": 20, "exit_window": 30})  # exit must be <= entry
    assert _valid({"entry_window": 55, "exit_window": 20})


def test_grids_cover_core_strategies() -> None:
    assert {"bollinger", "rsi_meanrevert", "ema_crossover", "atr_channel"} <= set(PARAM_GRIDS)


def test_optimize_ranks_by_objective(monkeypatch: pytest.MonkeyPatch) -> None:
    df = _synth()
    monkeypatch.setattr(opt.MarketData, "history", lambda self, **kw: df)
    results = optimize_parameters(
        object(), object(), strategy="bollinger", symbol="X", years=5.0, min_trades=1  # type: ignore[arg-type]
    )
    assert results, "expected at least one param combo to backtest"
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)  # ranked best-first
    assert all(np.isfinite(r.score) for r in results)
    assert all("window" in r.params and "n_std" in r.params for r in results)


def test_optimize_skips_invalid_crossovers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opt.MarketData, "history", lambda self, **kw: _synth())
    results = optimize_parameters(
        object(), object(), strategy="ema_crossover", symbol="X", min_trades=1  # type: ignore[arg-type]
    )
    assert all(r.params["fast"] < r.params["slow"] for r in results)
