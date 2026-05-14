from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sandbox state/log/cache dirs per test so journals don't leak between tests."""
    for var, sub in (("STATE_DIR", "state"), ("LOG_DIR", "logs"), ("DATA_CACHE_DIR", "data/cache")):
        path = tmp_path / sub
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(path))
    # Defang accidental live mode in test envs.
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    monkeypatch.setenv("QUESTRADE_ENV", "practice")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-key-do-not-use-in-production")
    monkeypatch.setenv("QUESTRADE_REFRESH_TOKEN", "test-refresh-token")
    # Refresh the cached Settings singleton.
    from trading_live_claude.config.settings import get_settings as gs

    gs.cache_clear()


@pytest.fixture()
def random_walk_df() -> pd.DataFrame:
    rng = np.random.default_rng(seed=42)
    n = 500
    rets = rng.normal(loc=0.0005, scale=0.015, size=n)
    close = 100.0 * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, 0.005, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.005, n)))
    open_ = close * (1 + rng.normal(0, 0.002, n))
    vol = rng.integers(100_000, 1_000_000, n)
    times = pd.date_range("2022-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {"time": times, "open": open_, "high": high, "low": low, "close": close, "volume": vol}
    )


@pytest.fixture()
def trending_df() -> pd.DataFrame:
    """Synthetic trending series so EMA-cross style strategies fire."""
    n = 400
    base = np.linspace(100, 200, n)
    noise = np.random.default_rng(0).normal(0, 0.5, n)
    close = base + noise
    high = close + 0.5
    low = close - 0.5
    open_ = close - 0.1
    vol = np.full(n, 500_000)
    times = pd.date_range("2022-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {"time": times, "open": open_, "high": high, "low": low, "close": close, "volume": vol}
    )
