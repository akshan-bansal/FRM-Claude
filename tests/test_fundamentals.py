from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from trading_live_claude.data.fundamentals import FundamentalsStore
from trading_live_claude.strategies.base import StrategyContext
from trading_live_claude.strategies.examples.bollinger import BollingerMeanRevert
from trading_live_claude.strategies.valuation import ValuationStrategy

CTX = StrategyContext(symbol="AAA")


def _df(n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, n)))
    return pd.DataFrame(
        {"time": pd.date_range("2021-01-01", periods=n, freq="B", tz="UTC"),
         "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": [1e6] * n}
    )


def _write(root: Path, sym: str, rows: list[tuple[str, float]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=["date", "bvps"]).to_csv(root / f"{sym.replace('.', '_')}.csv", index=False)


def test_store_forward_fills_quarterly(tmp_path: Path) -> None:
    df = _df(300)
    _write(tmp_path, "AAA", [("2021-01-01", 40.0), ("2021-04-01", 42.0), ("2021-07-01", 45.0)])
    s = FundamentalsStore(tmp_path).bvps_series(df, "AAA")
    assert s is not None and len(s) == len(df)
    assert s.iloc[0] == 40.0
    assert s.iloc[-1] == 45.0  # forward-filled to the latest quarter
    assert s.isna().sum() == 0


def test_none_without_file_or_dates(tmp_path: Path) -> None:
    assert FundamentalsStore(tmp_path).bvps_series(_df(), "MISSING") is None
    _write(tmp_path, "AAA", [("2021-01-01", 40.0)])
    assert FundamentalsStore(tmp_path).bvps_series(_df().drop(columns=["time"]), "AAA") is None


def test_valuation_diverges_from_price_with_varying_bvps(tmp_path: Path) -> None:
    # Book value that swings independently of price makes P/B != a scaled price, so the
    # mean-reversion signal on P/B genuinely differs from the same signal on price.
    df = _df(400)
    _write(tmp_path, "AAA", [("2020-12-01", 30.0), ("2021-06-01", 60.0), ("2021-12-01", 30.0),
                             ("2022-06-01", 70.0), ("2022-12-01", 35.0)])
    store = FundamentalsStore(tmp_path)
    price = BollingerMeanRevert(window=20, n_std=2.0).generate_signals(df, CTX)["entry"]
    val = ValuationStrategy(BollingerMeanRevert(window=20, n_std=2.0), store=store).generate_signals(df, CTX)["entry"]
    assert not price.equals(val)  # fundamentals actually changed the buy signal


def test_falls_back_to_price_without_fundamentals(tmp_path: Path) -> None:
    df = _df(400)
    store = FundamentalsStore(tmp_path)  # empty — no file for AAA
    price = BollingerMeanRevert(window=20, n_std=2.0).generate_signals(df, CTX)["entry"]
    val = ValuationStrategy(BollingerMeanRevert(window=20, n_std=2.0), store=store).generate_signals(df, CTX)["entry"]
    assert price.equals(val)  # no book value → P/B collapses to price → identical signal
