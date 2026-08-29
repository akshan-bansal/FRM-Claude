from __future__ import annotations

import numpy as np
import pandas as pd

from trading_live_claude.portfolio import PortfolioAllocator, build_book, ranker_scores


def _universe(n_names: int = 16, days: int = 900, seed: int = 0) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    t = pd.date_range("2021-01-01", periods=days, freq="B", tz="UTC")
    quality = rng.normal(0.0, 0.0009, n_names)
    out = {}
    for i in range(n_names):
        c = 100.0 * np.exp(np.cumsum(quality[i] + rng.normal(0.0, 0.012, days)))
        out[f"N{i:02d}"] = pd.DataFrame({"time": t, "open": c, "high": c * 1.005,
                                         "low": c * 0.995, "close": c, "volume": 1e6})
    return out


def test_ranker_scores_cover_the_universe() -> None:
    uni = _universe()
    scores = ranker_scores(uni, horizon=21)
    assert set(scores).issubset(set(uni)) and len(scores) > 0
    assert all(np.isfinite(v) for v in scores.values())


def test_build_book_weights_only_positive_edge_names() -> None:
    uni = _universe(seed=1)
    res = build_book(uni, horizon=21)
    assert res.weights and abs(sum(res.weights.values()) - res.gross_exposure) < 1e-9
    # the allocator only funds positive-edge names
    scores = ranker_scores(uni, horizon=21)
    assert all(scores[name] > 0.0 for name in res.weights)


def test_regime_benchmark_scales_gross() -> None:
    uni = _universe(seed=2)
    bench = uni["N00"]["close"]              # any broad series as the regime proxy
    res = build_book(uni, horizon=21, regime_benchmark=bench,
                     allocator=PortfolioAllocator(max_weight=0.2))
    assert res.gross_exposure <= 1.0 and res.cash >= 0.0
    assert max(res.weights.values(), default=0.0) <= 0.2 + 1e-9
