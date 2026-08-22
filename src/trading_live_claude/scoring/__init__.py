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
from .routing import (
    RouteEntry,
    assets_for_strategy,
    render_routing_markdown,
    route_symbols_to_strategies,
    strategy_asset_plan,
    to_strategy_map,
    to_strategy_map_string,
)
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
    "RouteEntry",
    "Scorer",
    "ScorerConfig",
    "StrategyScore",
    "ThresholdChoice",
    "assets_for_strategy",
    "calibrate_threshold",
    "cell_objective_input",
    "combine_scores",
    "family_of",
    "get_objective",
    "rank_cells",
    "rank_qc_library",
    "register_objective",
    "render_combined_scoreboard",
    "render_routing_markdown",
    "render_scoreboard_markdown",
    "route_symbols_to_strategies",
    "score_strategies",
    "select_portfolio",
    "stats_to_objective_input",
    "strategy_asset_plan",
    "to_strategy_map",
    "to_strategy_map_string",
]
