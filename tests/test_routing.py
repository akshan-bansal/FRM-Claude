from __future__ import annotations

from trading_live_claude.analysis.matrix import MatrixCell
from trading_live_claude.scoring.routing import (
    assets_for_strategy,
    route_symbols_to_strategies,
    strategy_asset_plan,
    to_strategy_map,
    to_strategy_map_string,
)


def _cell(strategy: str, symbol: str, precision: float, recall: float = 0.1) -> MatrixCell:
    return MatrixCell(
        strategy=strategy, symbol=symbol, recall=recall, specificity=0.9, precision=precision,
        max_drawdown=-0.1, num_trades=20, support=500, roc_auc=0.6, fidelity=0.2,
    )


def _cells() -> list[MatrixCell]:
    return [
        _cell("bollinger", "XIC.TO", 0.30), _cell("ts_momentum", "XIC.TO", 0.60),  # XIC → ts_momentum
        _cell("bollinger", "SU.TO", 0.70), _cell("ts_momentum", "SU.TO", 0.20),    # SU → bollinger
        _cell("bollinger", "RY.TO", 0.40), _cell("ts_momentum", "RY.TO", 0.55),    # RY → ts_momentum
    ]


def test_route_symbols_picks_best_strategy_per_symbol() -> None:
    routing = route_symbols_to_strategies(_cells(), objective="precision")
    sm = to_strategy_map(routing)
    assert sm["XIC.TO"] == "ts_momentum"
    assert sm["SU.TO"] == "bollinger"
    assert sm["RY.TO"] == "ts_momentum"


def test_route_respects_min_score() -> None:
    # Floor above the weakest symbol's best score drops it from the routing.
    routing = route_symbols_to_strategies(_cells(), objective="precision", min_score=0.5)
    # SU best=0.70, XIC best=0.60, RY best=0.55 all pass 0.5; raise floor:
    routing2 = route_symbols_to_strategies(_cells(), objective="precision", min_score=0.65)
    assert set(routing) == {"XIC.TO", "SU.TO", "RY.TO"}
    assert set(routing2) == {"SU.TO"}  # only SU (0.70) clears 0.65


def test_assets_for_strategy_ranks_symbols() -> None:
    top = assets_for_strategy(_cells(), "bollinger", objective="precision", top_n=2)
    assert [e.symbol for e in top] == ["SU.TO", "RY.TO"]  # 0.70 > 0.40


def test_strategy_asset_plan_covers_all_strategies() -> None:
    plan = strategy_asset_plan(_cells(), objective="precision", top_n=1)
    assert set(plan) == {"bollinger", "ts_momentum"}
    assert plan["bollinger"][0].symbol == "SU.TO"
    assert plan["ts_momentum"][0].symbol == "XIC.TO"


def test_strategy_map_string_is_monitor_ready() -> None:
    s = to_strategy_map_string(route_symbols_to_strategies(_cells(), objective="precision"))
    assert "XIC.TO=ts_momentum" in s and "SU.TO=bollinger" in s
    assert s.count(",") == 2  # three routed symbols → two commas
