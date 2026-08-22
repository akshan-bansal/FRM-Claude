"""Scoring / objective layer — the *precision* stage of the pipeline.

``objective`` provides swappable objective functions (a named registry + adapter)
so the tuner and scorer can optimize for precision, recall, an F-beta blend, or the
legacy Sharpe/DD score purely by changing a config string.
"""
from __future__ import annotations

from .objective import (
    OBJECTIVES,
    ObjectiveAdapter,
    ObjectiveInput,
    get_objective,
    register_objective,
)
from .qc_bridge import QcBacktestScore, rank_qc_library, stats_to_objective_input
from .scorer import Scorer, ScorerConfig, ThresholdChoice, calibrate_threshold
from .selection import (
    STRATEGY_FAMILY,
    CombinedScore,
    StrategyScore,
    cell_objective_input,
    combine_scores,
    family_of,
    rank_cells,
    render_combined_scoreboard,
    render_scoreboard_markdown,
    score_strategies,
    select_portfolio,
)

__all__ = [
    "OBJECTIVES",
    "STRATEGY_FAMILY",
    "CombinedScore",
    "ObjectiveAdapter",
    "ObjectiveInput",
    "QcBacktestScore",
    "Scorer",
    "ScorerConfig",
    "StrategyScore",
    "ThresholdChoice",
    "calibrate_threshold",
    "cell_objective_input",
    "combine_scores",
    "family_of",
    "get_objective",
    "rank_cells",
    "rank_qc_library",
    "register_objective",
    "render_combined_scoreboard",
    "render_scoreboard_markdown",
    "score_strategies",
    "select_portfolio",
    "stats_to_objective_input",
]
