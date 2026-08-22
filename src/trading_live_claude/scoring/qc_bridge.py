"""Bridge QC-library strategies into the multi-scoring engine.

This is the 'expand scoring from the QC library reader' path: for each of your QC
projects, read its latest cloud backtest statistics and rank them through the same
swappable ``ObjectiveAdapter`` used for the native pipeline. QC LEAN backtests don't
carry our recall/precision (different framework), so we score on their P&L stats
(Sharpe / drawdown) — the ``sharpe_over_dd`` objective works directly.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..integrations.qc_library import analyze_source, categorize_source
from ..integrations.quantconnect import QuantConnectClient
from .objective import ObjectiveAdapter, ObjectiveInput


def _parse_float(v: object) -> float:
    """Parse a QC statistic ('-0.472', '24.400%', '$1,000') into a float."""
    s = str(v).strip().replace("$", "").replace(",", "").replace("%", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def stats_to_objective_input(stats: dict[str, object]) -> ObjectiveInput:
    """Map a QC backtest statistics dict to an ObjectiveInput (P&L only)."""
    sharpe = _parse_float(stats.get("Sharpe Ratio", 0.0))
    drawdown = -abs(_parse_float(stats.get("Drawdown", 0.0)) / 100.0)  # QC gives % → negative fraction
    return ObjectiveInput(sharpe=sharpe, max_drawdown=drawdown)


@dataclass(frozen=True)
class QcBacktestScore:
    project_id: int
    name: str
    family: str
    sharpe: float
    drawdown: float
    objective_value: float


def _latest_backtest_id(backtests: list[dict[str, object]]) -> str:
    """Pick the most recent completed backtest id (fallback: last listed)."""
    completed = [b for b in backtests if b.get("completed") or b.get("status") == "Completed."]
    pool = completed or backtests
    if not pool:
        return ""
    return str(pool[-1].get("backtestId", ""))


def rank_qc_library(
    client: QuantConnectClient,
    *,
    objective: str = "sharpe_over_dd",
) -> list[QcBacktestScore]:
    """Score each QC project by its latest backtest, ranked by ``objective`` desc.

    Projects with no backtest are skipped. Family is detected from source code so
    QC strategies land in the same taxonomy as native ones (for diversification).
    """
    adapter = ObjectiveAdapter.from_name(objective)
    scores: list[QcBacktestScore] = []
    for project in client.list_projects():
        pid = int(str(project.get("projectId", 0)) or 0)
        name = str(project.get("name", ""))
        backtests = client.list_backtests(pid)
        bt_id = _latest_backtest_id(backtests)
        if not bt_id:
            continue
        result = client.read_backtest(pid, bt_id)
        backtest = result.get("backtest", {})
        stats = backtest.get("statistics", {}) if isinstance(backtest, dict) else {}
        if not isinstance(stats, dict) or not stats:
            continue
        oi = stats_to_objective_input(stats)
        # Family: prefer source-code detection, fall back to the project name.
        try:
            family = analyze_source(pid, name, _pull(client, pid)).family
        except Exception:
            family = categorize_source("", name)
        scores.append(
            QcBacktestScore(
                project_id=pid,
                name=name,
                family=family,
                sharpe=oi.sharpe,
                drawdown=oi.max_drawdown,
                objective_value=adapter.score(oi),
            )
        )
    scores.sort(key=lambda s: s.objective_value, reverse=True)
    return scores


def _pull(client: QuantConnectClient, project_id: int) -> dict[str, str]:
    files = client.list_files(project_id)
    return {str(f.get("name", "")): str(f.get("content", "")) for f in files if isinstance(f, dict)}
