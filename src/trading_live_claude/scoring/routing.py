"""Strategy-conditioned asset selection and routing.

Two complementary views over the scored signal matrix, both driven by the swappable
objective (dot_product, roc_auc, …) so the routing target is configurable:

* **Asset selection** (``strategy_asset_plan`` / ``assets_for_strategy``) — for each
  strategy, the assets it actually has an edge on. Answers "where should I run
  zscore_ou?".
* **Routing** (``route_symbols_to_strategies`` / ``to_strategy_map``) — for each
  symbol, the single best strategy to trade it with. Answers "for XIC.TO, which
  strategy?". The result feeds the monitor's per-symbol ``--strategy-map`` directly.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..analysis.matrix import MatrixCell
from .objective import ObjectiveAdapter, ObjectiveFn
from .selection import cell_objective_input, family_of


@dataclass(frozen=True)
class RouteEntry:
    symbol: str
    strategy: str
    family: str
    score: float


def _score_fn(objective: str | ObjectiveFn) -> ObjectiveFn:
    return objective if callable(objective) else ObjectiveAdapter.from_name(objective).score


def route_symbols_to_strategies(
    cells: Iterable[MatrixCell],
    *,
    objective: str | ObjectiveFn = "dot_product",
    min_score: float | None = None,
) -> dict[str, RouteEntry]:
    """Route each symbol to its single highest-scoring strategy.

    Symbols whose best score is below ``min_score`` are dropped (unrouted), so a
    symbol no strategy has an edge on is simply not traded.
    """
    fn = _score_fn(objective)
    best: dict[str, RouteEntry] = {}
    for c in cells:
        score = fn(cell_objective_input(c))
        if min_score is not None and score < min_score:
            continue
        current = best.get(c.symbol)
        if current is None or score > current.score:
            best[c.symbol] = RouteEntry(c.symbol, c.strategy, family_of(c.strategy), score)
    return best


def assets_for_strategy(
    cells: Iterable[MatrixCell],
    strategy: str,
    *,
    objective: str | ObjectiveFn = "dot_product",
    top_n: int = 5,
    min_score: float | None = None,
) -> list[RouteEntry]:
    """The top ``top_n`` assets a single strategy has an edge on, ranked by ``objective``."""
    fn = _score_fn(objective)
    entries: list[RouteEntry] = []
    for c in cells:
        if c.strategy != strategy:
            continue
        score = fn(cell_objective_input(c))
        if min_score is not None and score < min_score:
            continue
        entries.append(RouteEntry(c.symbol, strategy, family_of(strategy), score))
    entries.sort(key=lambda e: e.score, reverse=True)
    return entries[:top_n]


def strategy_asset_plan(
    cells: Iterable[MatrixCell],
    *,
    objective: str | ObjectiveFn = "dot_product",
    top_n: int = 3,
    min_score: float | None = None,
) -> dict[str, list[RouteEntry]]:
    """For every strategy in the matrix, its top ``top_n`` assets (asset selection)."""
    cell_list = list(cells)
    strategies = sorted({c.strategy for c in cell_list})
    return {
        s: assets_for_strategy(cell_list, s, objective=objective, top_n=top_n, min_score=min_score)
        for s in strategies
    }


def to_strategy_map(routing: dict[str, RouteEntry]) -> dict[str, str]:
    """Collapse a symbol→RouteEntry routing into a {symbol: strategy} map."""
    return {symbol: entry.strategy for symbol, entry in routing.items()}


def to_strategy_map_string(routing: dict[str, RouteEntry]) -> str:
    """Render the routing as a 'SYM=strategy,…' string for `trading signal --strategy-map`."""
    return ",".join(f"{sym}={entry.strategy}" for sym, entry in sorted(routing.items()))


def render_routing_markdown(routing: dict[str, RouteEntry]) -> str:
    """Render the per-symbol routing as a markdown table, best score first."""
    if not routing:
        return "# Routing\n\n_No symbols routed (none cleared the score floor)._\n"
    ordered = sorted(routing.values(), key=lambda e: e.score, reverse=True)
    header = (
        "# Strategy routing — symbol → best strategy\n\n"
        "| Symbol | Strategy | Family | Score |\n|---|---|---|---:|\n"
    )
    rows = [f"| {e.symbol} | {e.strategy} | {e.family} | {e.score:.3f} |" for e in ordered]
    return header + "\n".join(rows) + "\n"
