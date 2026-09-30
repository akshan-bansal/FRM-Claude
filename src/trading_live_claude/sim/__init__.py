"""OASIS simulation boundary: a seed goes out, a result comes back, nothing else crosses.

The simulation runs on a separate VM and is opaque (OASIS's agent graph has no adapter hooks). This
package owns only the two files that cross the boundary and the checks on them. It never imports the
simulator, never reaches the Router, and never sees an API key.
"""
from .contract import RunBudget, SimResult, SimSeed, build_seed, ingest_result

__all__ = ["RunBudget", "SimResult", "SimSeed", "build_seed", "ingest_result"]
