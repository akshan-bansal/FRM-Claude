from __future__ import annotations

from trading_live_claude.analysis.matrix import MatrixCell
from trading_live_claude.scoring.qc_bridge import QcBacktestScore
from trading_live_claude.scoring.selection import (
    combine_scores,
    render_combined_scoreboard,
    render_scoreboard_markdown,
    score_strategies,
    select_portfolio,
)


def _cell(
    strategy: str, symbol: str, precision: float, recall: float, dd: float = -0.1, specificity: float = 0.9
) -> MatrixCell:
    return MatrixCell(
        strategy=strategy, symbol=symbol, recall=recall, specificity=specificity, precision=precision,
        max_drawdown=dd, num_trades=20, support=500,
    )


def _sample_cells() -> list[MatrixCell]:
    return [
        # high precision, low recall
        _cell("bollinger", "A", 0.80, 0.20), _cell("bollinger", "B", 0.70, 0.25),
        # low precision, high recall
        _cell("ts_momentum", "A", 0.40, 0.80), _cell("ts_momentum", "B", 0.45, 0.75),
        # balanced
        _cell("atr_channel", "A", 0.55, 0.55), _cell("atr_channel", "B", 0.60, 0.50),
    ]


def test_aggregates_per_strategy() -> None:
    scores = score_strategies(_sample_cells(), objective="precision")
    by = {s.strategy: s for s in scores}
    assert by["bollinger"].coverage == 2
    assert by["bollinger"].mean_precision == 0.75  # (0.80 + 0.70) / 2
    assert by["bollinger"].family == "mean_reversion"
    assert by["ts_momentum"].family == "momentum"


def test_precision_objective_ranks_bollinger_first() -> None:
    scores = score_strategies(_sample_cells(), objective="precision")
    assert scores[0].strategy == "bollinger"  # highest mean precision


def test_recall_objective_reranks_momentum_first() -> None:
    # Swapping the objective must change the winner — the multi-scoring claim.
    scores = score_strategies(_sample_cells(), objective="recall")
    assert scores[0].strategy == "ts_momentum"  # highest mean recall


def test_select_portfolio_caps_per_family() -> None:
    cells = [
        _cell("bollinger", "A", 0.9, 0.3), _cell("rsi2_connors", "A", 0.85, 0.3),
        _cell("zscore_ou", "A", 0.8, 0.3),  # three mean-reversion
        _cell("ts_momentum", "A", 0.75, 0.6),  # one momentum
    ]
    scores = score_strategies(cells, objective="precision")
    picked = select_portfolio(scores, top_k=3, max_per_family=2)
    families = [p.family for p in picked]
    assert families.count("mean_reversion") <= 2  # cap enforced
    assert "momentum" in families  # diversified in despite lower precision


def test_select_portfolio_respects_top_k() -> None:
    scores = score_strategies(_sample_cells(), objective="f_beta")
    assert len(select_portfolio(scores, top_k=2, max_per_family=5)) == 2


def test_scoreboard_markdown_renders() -> None:
    md = render_scoreboard_markdown(score_strategies(_sample_cells(), objective="f_beta"))
    assert "Strategy scoreboard" in md
    assert "Family" in md


def test_empty_scoreboard_graceful() -> None:
    assert "No strategies" in render_scoreboard_markdown([])


def test_combine_folds_native_and_qc() -> None:
    native = score_strategies(_sample_cells(), objective="precision")
    qc = [
        QcBacktestScore(project_id=9, name="Bollinger Bot", family="mean_reversion",
                        sharpe=2.0, drawdown=-0.10, objective_value=20.0),
    ]
    combined = combine_scores(native, qc)
    sources = {c.source for c in combined}
    assert sources == {"native", "qc"}
    assert len(combined) == len(native) + 1
    # sorted by objective_value desc → the QC row (20.0) leads the precision rows (<1).
    assert combined[0].source == "qc"


def test_combined_scoreboard_shows_both_bases() -> None:
    native = score_strategies(_sample_cells(), objective="precision")
    qc = [QcBacktestScore(1, "QC A", "momentum", 1.0, -0.2, 5.0)]
    md = render_combined_scoreboard(combine_scores(native, qc))
    assert "native" in md and "qc" in md
    assert "Objective" in md


def test_combined_empty_graceful() -> None:
    assert "No strategies" in render_combined_scoreboard([])
