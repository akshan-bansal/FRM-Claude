"""OASIS simulation boundary: a seed goes out, a result comes back, nothing else crosses.

The simulation runs on a separate VM and is opaque (OASIS's agent graph has no adapter hooks). This
package owns only the two files that cross the boundary and the checks on them. It never imports the
simulator, never reaches the Router, and never sees an API key.

``society`` (2026-10-03) is the pure layer on top of a validated result: it turns the reported
stances into ``holds_stance`` graph edges, maps a stance to an influence multiplier in (0, 1] that
can only reduce size, and runs the round-trip-cost check on the scaled intent. It places nothing.
``Router.society_view`` (off unless set) is the one seam through which a :class:`SocietyView` shrinks
an entry; nothing sets it in any launcher yet.
"""
from .contract import RunBudget, SimResult, SimSeed, build_seed, ingest_result
from .journal import is_journaled, journal_run, poll_id_for, run_edges
from .oasis_graph import (
    OasisRun,
    attention_matrix,
    herd_from_run,
    oasis_edges,
    read_oasis_db,
)
from .society import (
    AdapterParams,
    SocietyView,
    compose,
    cost_check_after_scaling,
    society_edges,
    society_influence,
    society_scaled_edge,
    top_eigenvalue_share,
)

__all__ = ["AdapterParams", "OasisRun", "RunBudget", "SimResult", "SimSeed", "SocietyView",
           "attention_matrix", "build_seed", "compose", "cost_check_after_scaling",
           "herd_from_run", "ingest_result", "is_journaled", "journal_run", "oasis_edges",
           "poll_id_for", "read_oasis_db", "run_edges", "society_edges",
           "society_influence", "society_scaled_edge", "top_eigenvalue_share"]
