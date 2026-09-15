"""Graph-weighted interpretation: a thesis earns or loses confidence from the intel graph's history.

``interpret()`` reads one snapshot. The graph journal holds every read, so it can say whether a
thesis's domains have stayed elevated across consecutive polls or were quiet in the latest one. A
thesis whose domains persisted ``min_polls`` polls moves up one confidence notch; one with no domain
elevated in the latest poll moves down one. Confidence only sets how hard interpret trims entries —
it still never boosts or blocks — and a graph that cannot be read leaves theses unchanged.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from ..logging_setup import get_logger
from .graph import DEFAULT_GRAPH_JOURNAL, Edge, edge_persistence, load_edges
from .interpret import Thesis, interpret
from .overlay import IntelSnapshot

log = get_logger(__name__)

# Which graph domains (the persistence gate's watched set) bear on each interpret theme.
THEME_DOMAINS: Mapping[str, tuple[str, ...]] = {
    "energy": ("energy",),
    "defense_geopolitical": ("conflict", "military"),
    "safe_haven": ("conflict", "military", "disaster"),
    "volatility_convexity": ("economy", "conflict"),
    "materials": ("energy", "disaster"),
    "dollar": ("economy",),
    "insurance": ("disaster",),
    "emerging_markets": ("economy", "conflict"),
}
_LADDER = ("tentative", "moderate", "high")
_NULL_THESIS = "No notable configuration"


def domain_persistence(edges: Sequence[Edge], domains: Iterable[str]) -> dict[str, int]:
    return {d: edge_persistence(edges, predicate="elevated_in", object=("domain", d)) for d in set(domains)}


def weigh_by_graph(theses: Sequence[Thesis], persistence: Mapping[str, int], *,
                   min_polls: int) -> list[Thesis]:
    out: list[Thesis] = []
    for t in theses:
        domains = {d for theme in t.themes for d in THEME_DOMAINS.get(theme, ())}
        if t.name == _NULL_THESIS or not domains or t.confidence not in _LADDER:
            out.append(t)
            continue
        driver, run = max(((d, persistence.get(d, 0)) for d in sorted(domains)), key=lambda p: p[1])
        rank = _LADDER.index(t.confidence)
        if run >= min_polls:
            rank, note = min(rank + 1, len(_LADDER) - 1), f"graph: '{driver}' elevated {run} consecutive polls"
        elif run == 0:
            rank, note = max(rank - 1, 0), "graph: no theme domain elevated in the latest poll"
        else:
            note = f"graph: '{driver}' elevated {run} polls (< {min_polls})"
        out.append(replace(t, confidence=_LADDER[rank], evidence=[*t.evidence, note]))
    return out


class GraphInterpreter:
    """Zero-arg callable for ``LiveMonitor(interpret_for=...)``: theses from the latest snapshot,
    weighed by the graph journal (re-read at most every ``refresh_seconds``)."""

    def __init__(self, snapshot_for: Callable[[], IntelSnapshot | None], *, min_polls: int = 3,
                 refresh_seconds: float = 300.0, graph_path: str | Path = DEFAULT_GRAPH_JOURNAL,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._snapshot_for = snapshot_for
        self.min_polls = min_polls
        self._refresh = refresh_seconds
        self._graph_path = Path(graph_path)
        self._clock = clock
        self._persistence: dict[str, int] | None = None
        self._ts = 0.0

    def _graph(self) -> dict[str, int] | None:
        if self._persistence is None or self._clock() - self._ts >= self._refresh:
            try:
                edges = load_edges(self._graph_path)
                # An empty or missing journal is absence of evidence, not evidence of quiet.
                self._persistence = domain_persistence(
                    edges, (d for doms in THEME_DOMAINS.values() for d in doms)) if edges else None
            except Exception as e:
                log.warning("graph_interpret.graph_unreadable", error=str(e))
                self._persistence = None
            self._ts = self._clock()
        return self._persistence

    def __call__(self) -> list[Thesis]:
        snap = self._snapshot_for()
        if snap is None:
            return []
        theses = interpret(snap)
        persistence = self._graph()
        return theses if persistence is None else weigh_by_graph(theses, persistence,
                                                                 min_polls=self.min_polls)
