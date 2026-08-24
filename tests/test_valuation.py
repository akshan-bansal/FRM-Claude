from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.scoring.selection import family_of
from trading_live_claude.signals.valuation import (
    DEFAULT_BVPS,
    resolve_bvps,
    rolling_zscore,
    valuation_features,
    valuation_series,
)
from trading_live_claude.strategies import STRATEGIES
from trading_live_claude.strategies.base import StrategyContext
from trading_live_claude.strategies.examples.bollinger import BollingerMeanRevert
from trading_live_claude.strategies.valuation import (
    VALUATION_STRATEGIES,
    ValuationGateOverlay,
    ValuationStrategy,
    valuation_composite,
)

CTX = StrategyContext(symbol="T")


def _df_with_book(n: int = 400, bvps: float = 40.0) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.02, n)))
    return pd.DataFrame(
        {
            "time": pd.date_range("2022-01-01", periods=n, freq="B", tz="UTC"),
            "open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
            "volume": [1e6] * n, "bvps": [bvps] * n,
        }
    )


# ---- features -------------------------------------------------------------------

def test_price_to_book_and_book_to_market_are_reciprocal() -> None:
    df = _df_with_book()
    pb = valuation_series(df, "price_to_book")
    bm = valuation_series(df, "book_to_market")
    assert np.allclose((pb * bm).to_numpy(), 1.0)


def test_resolve_bvps_defaults_to_constant_without_column() -> None:
    df = _df_with_book().drop(columns=["bvps"])
    b = resolve_bvps(df)
    assert (b == DEFAULT_BVPS).all()
    # With no book value, P/B collapses to the price level.
    assert np.allclose(valuation_series(df, "price_to_book").to_numpy(), df["close"].to_numpy())


def test_rolling_zscore_is_backward_only() -> None:
    """The z-score at bar t is unchanged by truncating the series after t."""
    df = _df_with_book()
    s = valuation_series(df, "price_to_book")
    full = rolling_zscore(s)
    trunc = rolling_zscore(s.iloc[:300])
    pd.testing.assert_series_equal(
        full.iloc[200:300].reset_index(drop=True), trunc.iloc[200:300].reset_index(drop=True), check_names=False
    )


def test_valuation_features_columns() -> None:
    feats = valuation_features(_df_with_book())
    assert {"bvps", "pb", "bm", "pb_z", "bm_z"}.issubset(feats.columns)


def test_unknown_metric_rejected() -> None:
    with pytest.raises(ValueError):
        valuation_series(_df_with_book(), "market_cap")


# ---- valuation strategy ---------------------------------------------------------

def test_valuation_strategy_runs_meanrev_on_ratio() -> None:
    df = _df_with_book()
    strat = ValuationStrategy(BollingerMeanRevert(window=20, n_std=2.0), metric="price_to_book")
    out = strat.generate_signals(df, CTX)
    assert {"entry", "exit", "atr", "valuation_ratio"}.issubset(out.columns)
    assert out["entry"].dropna().isin([0, 1]).all()
    # The ratio the base saw is P/B = price / book, not the raw price.
    assert np.allclose(out["valuation_ratio"].to_numpy(), (df["close"] / df["bvps"]).to_numpy())


def test_valuation_strategy_no_lookahead_truncation() -> None:
    df = _df_with_book()
    strat = ValuationStrategy(BollingerMeanRevert(window=20, n_std=2.0))
    full = strat.generate_signals(df, CTX)["entry"].reset_index(drop=True)
    trunc = strat.generate_signals(df.iloc[:300].copy(), CTX)["entry"].reset_index(drop=True)
    pd.testing.assert_series_equal(full.iloc[200:300], trunc.iloc[200:300], check_names=False)


# ---- gate overlay (precision) ---------------------------------------------------

def test_gate_is_a_subset_of_base_entries() -> None:
    df = _df_with_book()
    base = BollingerMeanRevert(window=20, n_std=2.0)
    base_entries = base.generate_signals(df, CTX)["entry"].fillna(0).astype(int)
    gated = ValuationGateOverlay(base, z_window=40).generate_signals(df, CTX)["entry"].astype(int)
    assert gated.sum() <= base_entries.sum()
    assert bool(((gated == 1) <= (base_entries == 1)).all())


def test_gate_only_keeps_cheap_entries() -> None:
    """Every surviving entry must sit at or below the P/B trailing mean (z <= 0)."""
    df = _df_with_book()
    ov = ValuationGateOverlay(BollingerMeanRevert(window=20, n_std=2.0), z_window=40, max_z=0.0)
    out = ov.generate_signals(df, CTX)
    surviving = out[out["entry"] == 1]
    assert (surviving["valuation_z"] <= 0.0 + 1e-9).all()


# ---- composite (recall) ---------------------------------------------------------

def test_valuation_composite_unions_recall() -> None:
    df = _df_with_book()
    price = BollingerMeanRevert(window=20, n_std=2.0)
    comp = valuation_composite(price)
    price_entries = int(price.generate_signals(df, CTX)["entry"].fillna(0).sum())
    comp_entries = int(comp.generate_signals(df, CTX)["entry"].fillna(0).sum())
    assert comp_entries >= price_entries  # union can only widen


# ---- registration ---------------------------------------------------------------

def test_registered_and_family_and_zero_arg() -> None:
    assert set(VALUATION_STRATEGIES) == {"val_bollinger", "val_rsi", "val_gate_bollinger", "val_composite"}
    for name in VALUATION_STRATEGIES:
        assert name in STRATEGIES
        assert family_of(name) == "valuation"
        out = STRATEGIES[name]().generate_signals(_df_with_book(), CTX)
        assert {"entry", "exit", "atr"}.issubset(out.columns)
        assert STRATEGIES[name]().required_history_bars() >= 50
