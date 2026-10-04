"""Write a finished OASIS run into the intel graph journal, exactly once.

``state/intel_graph.jsonl`` already holds each poll's decomposition (``snapshot_to_edges``). This adds
the simulated society that read one of those polls:

* one ``seeded`` edge, poll -> society, so "which intelligence did this society read" is a walk;
* the run's reported stances (``holds_stance``, society -> symbol);
* when the run's database is supplied, its structure (``member_of`` / ``follows`` / ``engaged`` /
  ``attends``), read by :mod:`.oasis_graph`.

Two properties matter more than the edges themselves:

* **Once.** A run already in the journal is not written again, so re-running ingestion cannot double
  the record. The check is the ``seeded`` edge for that run id.
* **Not silent.** ``intel.graph.append_edges`` never raises (a journal write must not break a trading
  loop). That is the right contract on the trading path and the wrong one for an explicit ingest
  command, so this verifies the write landed and raises if it did not.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from ..intel.graph import DEFAULT_GRAPH_JOURNAL, Edge, append_edges, load_edges
from .contract import SimResult, SimSeed
from .oasis_graph import oasis_edges, read_oasis_db
from .society import society_edges


def poll_id_for(as_of: str) -> str:
    """The poll id ``snapshot_to_edges`` would have used for a snapshot taken at ``as_of``.

    That is ``datetime.isoformat()``; a seed carries the overlay row's ``as_of`` text, which can use a
    different spelling of the same instant (``Z`` vs ``+00:00``). Round-tripping through a datetime
    makes the two meet, so the ``seeded`` edge lands on the poll that actually exists. Text that is
    not a timestamp is used as given.
    """
    try:
        return datetime.fromisoformat(as_of.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return as_of


def run_edges(seed: SimSeed, result: SimResult, db_path: Path | None = None, *,
              aliases: Mapping[str, Sequence[str]] | None = None) -> list[Edge]:
    """Every edge for one run. ``db_path=None`` journals the aggregates only."""
    edges = [Edge(("poll", poll_id_for(seed.as_of)), "seeded", ("society", seed.run_id), weight=1.0,
                  as_of=seed.as_of,
                  meta={"run_id": seed.run_id, "model": result.model,
                        "agents": float(result.agents), "steps": float(result.completed_steps)})]
    edges += society_edges(seed, result)
    if db_path is not None:
        edges += oasis_edges(read_oasis_db(db_path), run_id=seed.run_id, as_of=seed.as_of,
                             symbols=seed.symbols, aliases=aliases)
    return edges


def is_journaled(path: str | Path, run_id: str) -> bool:
    """Whether ``run_id`` already has its ``seeded`` edge in the journal at ``path``."""
    return any(e.predicate == "seeded" and e.object == ("society", run_id) for e in load_edges(path))


def journal_run(seed: SimSeed, result: SimResult, db_path: Path | None = None, *,
                path: str | Path = DEFAULT_GRAPH_JOURNAL,
                aliases: Mapping[str, Sequence[str]] | None = None) -> int:
    """Append a run's edges to the journal once. Returns the number written (0 if already there).

    Raises ``RuntimeError`` when the write did not land. Builds the edges (and so reads the database)
    before touching the journal, so a bad database leaves the journal untouched.
    """
    if is_journaled(path, seed.run_id):
        return 0
    edges = run_edges(seed, result, db_path, aliases=aliases)
    append_edges(edges, path=path)
    if not is_journaled(path, seed.run_id):
        raise RuntimeError(f"run {seed.run_id}: writing {len(edges)} edges to {path} did not land "
                           "(append_edges logs and swallows its errors; see the log)")
    return len(edges)
