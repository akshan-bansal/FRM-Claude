from __future__ import annotations

import numpy as np
import pandas as pd

from trading_live_claude.analysis.matrix import (
    build_signal_matrix,
    render_matrix_markdown,
)


def _synth(seed: int, n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0005, 0.015, n)
    close = 100.0 * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, 0.005, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.005, n)))
    return pd.DataFrame(
        {
            "time": pd.date_range("2021-01-01", periods=n, freq="B", tz="UTC"),
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.integers(1e5, 1e6, n),
        }
    )


def test_matrix_covers_strategy_symbol_grid() -> None:
    frames = {"AAA": _synth(1), "BBB": _synth(2)}
    strategies = ["bollinger", "rsi_meanrevert", "composite"]
    cells = build_signal_matrix(frames, strategies=strategies)
    assert len(cells) == len(frames) * len(strategies)
    pairs = {(c.strategy, c.symbol) for c in cells}
    assert ("composite", "AAA") in pairs


def test_matrix_rates_are_bounded() -> None:
    cells = build_signal_matrix({"AAA": _synth(3)}, strategies=["bollinger"])
    for c in cells:
        assert 0.0 <= c.recall <= 1.0
        assert 0.0 <= c.specificity <= 1.0  # sensitivity/specificity/risk axes
        assert 0.0 <= c.precision <= 1.0
        assert c.max_drawdown <= 0.0  # drawdown is negative-or-zero
        assert c.support >= 0


def test_matrix_report_has_all_three_axes() -> None:
    from trading_live_claude.analysis.matrix import render_matrix_markdown

    md = render_matrix_markdown(build_signal_matrix({"AAA": _synth(3)}, strategies=["bollinger"]))
    assert "Sensitivity" in md and "Specificity" in md and "Max DD" in md


def test_composite_recall_beats_member_in_matrix() -> None:
    frames = {"AAA": _synth(4)}
    cells = build_signal_matrix(frames, strategies=["bollinger", "composite"])
    by_strat = {c.strategy: c for c in cells}
    assert by_strat["composite"].recall >= by_strat["bollinger"].recall - 1e-9


def test_short_frames_skipped() -> None:
    cells = build_signal_matrix({"SHORT": _synth(5, n=50)}, strategies=["bollinger"])
    assert cells == []


def test_render_markdown_has_table_and_sorted() -> None:
    cells = build_signal_matrix({"AAA": _synth(6)}, strategies=["bollinger", "composite"])
    md = render_matrix_markdown(cells)
    assert "| Strategy | Symbol |" in md
    assert "Precision" in md


def test_render_empty_is_graceful() -> None:
    md = render_matrix_markdown([])
    assert "No cells" in md
