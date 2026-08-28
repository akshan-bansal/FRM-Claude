from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.analysis.labeling import forward_return, label_events
from trading_live_claude.strategies import STRATEGIES
from trading_live_claude.strategies.base import StrategyContext


def _df(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    close = np.array(closes, dtype=float)
    return pd.DataFrame(
        {
            "time": pd.date_range("2022-01-01", periods=n, freq="B", tz="UTC"),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": np.full(n, 1_000),
        }
    )


def test_forward_return_is_exact() -> None:
    df = _df([100, 110, 121])
    fr = forward_return(df["close"], horizon=1)
    assert fr.iloc[0] == pytest.approx(0.10)
    assert fr.iloc[1] == pytest.approx(0.10)
    assert pd.isna(fr.iloc[2])  # no forward window on the last bar


def test_label_events_threshold() -> None:
    # +10% moves over horizon=1; threshold 3% => first two bars are positive.
    df = _df([100, 110, 121, 121.5])
    labels = label_events(df, horizon=1, up_threshold=0.03)
    assert labels.iloc[0] == 1
    assert labels.iloc[1] == 1
    assert labels.iloc[2] == 0  # 121 -> 121.5 is < 3%
    assert pd.isna(labels.iloc[3])  # trailing bar unknown, never a silent 0


def test_trailing_bars_are_na_not_zero() -> None:
    df = _df([100.0] * 20)
    labels = label_events(df, horizon=5, up_threshold=0.03)
    assert labels.iloc[-5:].isna().all()
    assert labels.iloc[:-5].notna().all()


def test_bad_horizon_rejected() -> None:
    with pytest.raises(ValueError):
        forward_return(pd.Series([1.0, 2.0]), horizon=0)


def test_labels_never_leak_into_features(random_walk_df: pd.DataFrame) -> None:
    """Lookahead guard: a Strategy must never receive a forward-looking label column.

    Labels are evaluation-only. If a strategy's output ever contained a 'label' or
    'forward_return' column, that would mean the future had leaked into features.
    """
    for name, cls in STRATEGIES.items():
        if name in {"pairs", "kalman_pairs"}:  # need a partner-leg column, not a single-symbol frame
            continue
        out = cls().generate_signals(random_walk_df, StrategyContext(symbol="T"))
        assert "label" not in out.columns
        assert "forward_return" not in out.columns
