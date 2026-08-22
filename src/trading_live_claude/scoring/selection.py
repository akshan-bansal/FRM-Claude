"""Portfolio-level strategy selection — the multi-scoring engine.

Where ``analysis.matrix`` scores each ``(strategy, symbol)`` cell, this module lifts
that to the **strategy** level: it aggregates a strategy's cells across the universe,
scores the aggregate through the swappable ``ObjectiveAdapter`` (so the selection
target — precision, recall, F-beta, expected value, … — is a config string), ranks
strategies, and picks a diversified portfolio that caps concentration per family.

That is what makes selection "multi-scoring": the same strategies re-rank differently
under different objectives, and the winner set respects family diversification.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..analysis.matrix import MatrixCell
from .objective import ObjectiveAdapter, ObjectiveFn, ObjectiveInput
from .qc_bridge import QcBacktestScore

# Family taxonomy for diversification. Every registered strategy maps to one family.
STRATEGY_FAMILY: dict[str, str] = {
    "bollinger": "mean_reversion",
    "rsi_meanrevert": "mean_reversion",
    "rsi2_connors": "mean_reversion",
    "zscore_ou": "mean_reversion",
    "bb_rsi_combo": "mean_reversion",
    "pairs": "mean_reversion",
    "ema_crossover": "momentum",
    "macd": "momentum",
    "momentum_breakout": "momentum",
    "ts_momentum": "momentum",
    "dual_ma": "momentum",
    "high_52w_breakout": "momentum",
    "atr_channel": "volatility",
    "bbwidth_squeeze": "volatility",
    "vol_target": "volatility",
    "turn_of_month": "seasonality",
    "day_of_week": "seasonality",
    "month_of_year": "seasonality",
    "composite": "composite",
}


def family_of(strategy: str) -> str:
    return STRATEGY_FAMILY.get(strategy, "other")


@dataclass(frozen=True)
class StrategyScore:
    strategy: str
    family: str
    objective: str
    objective_value: float
    mean_precision: float
    mean_recall: float
    worst_drawdown: float
    coverage: int  # number of symbols the strategy was scored over


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def score_strategies(
    cells: Iterable[MatrixCell],
    *,
    objective: str = "f_beta",
) -> list[StrategyScore]:
    """Aggregate matrix cells per strategy and rank by ``objective`` (desc).

    The aggregate feeds one ``ObjectiveInput`` per strategy: mean precision/recall
    across its symbols, and the *worst* drawdown (conservative risk). Ties break by
    mean precision then coverage so the ordering is deterministic.
    """
    adapter = ObjectiveAdapter.from_name(objective)
    by_strategy: dict[str, list[MatrixCell]] = {}
    for cell in cells:
        by_strategy.setdefault(cell.strategy, []).append(cell)

    scores: list[StrategyScore] = []
    for strategy, group in by_strategy.items():
        precisions = [c.precision for c in group]
        recalls = [c.recall for c in group]
        worst_dd = min((c.max_drawdown for c in group), default=0.0)
        mean_p, mean_r = _mean(precisions), _mean(recalls)
        value = adapter.score(
            ObjectiveInput(
                sharpe=0.0,
                max_drawdown=worst_dd,
                precision=mean_p,
                recall=mean_r,
            )
        )
        scores.append(
            StrategyScore(
                strategy=strategy,
                family=family_of(strategy),
                objective=objective,
                objective_value=value,
                mean_precision=mean_p,
                mean_recall=mean_r,
                worst_drawdown=worst_dd,
                coverage=len(group),
            )
        )

    scores.sort(key=lambda s: (s.objective_value, s.mean_precision, s.coverage), reverse=True)
    return scores


def select_portfolio(
    scores: list[StrategyScore],
    *,
    top_k: int = 5,
    max_per_family: int = 2,
) -> list[StrategyScore]:
    """Greedily pick the top ``top_k`` strategies, capping ``max_per_family`` each.

    ``scores`` is assumed already ranked (as ``score_strategies`` returns). The
    family cap forces diversification so a single family can't dominate the book.
    """
    picked: list[StrategyScore] = []
    family_counts: dict[str, int] = {}
    for s in scores:
        if len(picked) >= top_k:
            break
        if family_counts.get(s.family, 0) >= max_per_family:
            continue
        picked.append(s)
        family_counts[s.family] = family_counts.get(s.family, 0) + 1
    return picked


@dataclass(frozen=True)
class CombinedScore:
    """One row of a unified board spanning native strategies and QC-library ones."""

    source: str  # "native" | "qc"
    name: str
    family: str
    objective: str
    objective_value: float
    risk: float  # drawdown (negative fraction)
    detail: str


def combine_scores(
    native: list[StrategyScore], qc: list[QcBacktestScore]
) -> list[CombinedScore]:
    """Fold native + QC-library scores into one list, tagged by source.

    Native strategies are scored on signal-quality objectives (precision/recall);
    QC-library strategies on their own backtest P&L (Sharpe/DD). The two bases are
    not directly comparable, so the ``objective`` column stays visible — this is a
    unified *inventory* view for diversification, not a single common ranking.
    """
    combined: list[CombinedScore] = []
    for s in native:
        combined.append(
            CombinedScore(
                source="native", name=s.strategy, family=s.family, objective=s.objective,
                objective_value=s.objective_value, risk=s.worst_drawdown,
                detail=f"P={s.mean_precision:.0%} R={s.mean_recall:.0%}",
            )
        )
    for q in qc:
        combined.append(
            CombinedScore(
                source="qc", name=q.name, family=q.family, objective="sharpe_over_dd",
                objective_value=q.objective_value, risk=q.drawdown,
                detail=f"Sharpe={q.sharpe:.2f}",
            )
        )
    combined.sort(key=lambda c: c.objective_value, reverse=True)
    return combined


def render_combined_scoreboard(combined: list[CombinedScore]) -> str:
    """Render the unified native+QC board as a markdown table."""
    if not combined:
        return "# Combined scoreboard\n\n_No strategies to show._\n"
    header = (
        "# Combined scoreboard — native strategies + QC library\n\n"
        "_Native rows scored on signal quality; QC rows on backtest P&L — see the "
        "Objective column; values are not cross-comparable._\n\n"
        "| Source | Name | Family | Objective | Value | Risk | Detail |\n"
        "|---|---|---|---|---:|---:|---|\n"
    )
    rows = [
        f"| {c.source} | {c.name[:32]} | {c.family} | {c.objective} "
        f"| {c.objective_value:.3f} | {c.risk:.2%} | {c.detail} |"
        for c in combined
    ]
    return header + "\n".join(rows) + "\n"


def cell_objective_input(cell: MatrixCell) -> ObjectiveInput:
    """Adapt a MatrixCell into an ObjectiveInput so any objective can score it."""
    return ObjectiveInput(
        sharpe=0.0,
        max_drawdown=cell.max_drawdown,
        precision=cell.precision,
        recall=cell.recall,
        specificity=cell.specificity,
        roc_auc=cell.roc_auc,
        fidelity=cell.fidelity,
        cvar=cell.cvar,
    )


def rank_cells(
    cells: Iterable[MatrixCell], *, objective: str | ObjectiveFn = "dot_product"
) -> list[tuple[MatrixCell, float]]:
    """Rank matrix cells by an objective, desc.

    ``objective`` may be a registered name (dot_product, roc_auc, …) or a callable —
    e.g. a custom-weighted ``make_dot_product({...})`` for bespoke axis weighting.
    """
    score_fn: ObjectiveFn = objective if callable(objective) else ObjectiveAdapter.from_name(objective).score
    scored = [(c, score_fn(cell_objective_input(c))) for c in cells]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def render_scoreboard_markdown(scores: list[StrategyScore]) -> str:
    """Render the ranked strategies as a markdown table."""
    if not scores:
        return "# Strategy scoreboard\n\n_No strategies scored._\n"
    header = (
        f"# Strategy scoreboard — objective: {scores[0].objective}\n\n"
        "| Rank | Strategy | Family | Objective | Mean Prec | Mean Rec | Worst DD | Symbols |\n"
        "|---:|---|---|---:|---:|---:|---:|---:|\n"
    )
    rows = [
        f"| {i} | {s.strategy} | {s.family} | {s.objective_value:.4f} "
        f"| {s.mean_precision:.2%} | {s.mean_recall:.2%} | {s.worst_drawdown:.2%} | {s.coverage} |"
        for i, s in enumerate(scores, start=1)
    ]
    return header + "\n".join(rows) + "\n"
