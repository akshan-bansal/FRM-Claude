"""``symbol --loads_on--> factor`` edges from the factor models (L2's output, in graph form).

The only module in ``analysis`` that imports the graph, kept apart from ``factors.py`` so the
statistics stay free of it. It builds edges and returns them. It never writes: appending to the
journal (``intel.graph.append_edges``) and deciding what triggers it are separate steps, and no
launcher does either today.

Envelope, as everywhere else in the graph (``HOMOGENEOUS_GRAPH_SCOPE.md`` section 4):

* ``subject`` is ``("symbol", s)``, ``object`` is ``("factor", id)`` and ``weight`` is the loading in
  the factor kind's own unit (a beta for ``named``, a correlation loading for ``eigen`` and
  ``society``). Three kinds, one node type, told apart by ``meta.kind``.
* ``as_of`` is the DATA's time, passed in and required. For a society factor that is the seed's
  ``as_of``, not the moment the run finished. The wall clock is never read.
* ``influence`` stays ``None``. A loading is a measurement, not a multiplier in ``(0, 1]``.
* ``meta`` records the estimator and every parameter that shaped the number. For ``eigen`` that is
  the pinned Marchenko-Pastur spec, the noise variance and the thresholds, so which estimator
  produced a loading can be read off the edge. ``loads_on`` edges decay (72 h half-life) under
  ``DEFAULT_POLICIES``; nothing here changes that.
"""
from __future__ import annotations

import math
from datetime import datetime

from ..intel.graph import Edge
from .factors import EigenFactorModel, FactorFit, SocietyFactor

Meta = dict[str, float | str]


def eigen_factor_id(rank: int) -> str:
    return f"eigen:{rank}"


def named_factor_id(name: str) -> str:
    return f"named:{name}"


def society_factor_id(run_id: str) -> str:
    return f"society:{run_id}"


def _as_of(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("as_of is required: stamp edges with the data's own time, never the wall clock")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"as_of must be ISO-8601, got {value!r}") from exc
    if stamp.tzinfo is None:
        raise ValueError(f"as_of must carry a timezone, got {value!r}")
    return value


def _loading(value: float, what: str) -> float:
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{what} is not finite")
    return v


def _symbols(symbols: tuple[str, ...]) -> tuple[str, ...]:
    if len(set(symbols)) != len(symbols) or not all(symbols):
        raise ValueError("symbols must be unique and non-empty")
    return symbols


def _edge(symbol: str, factor_id: str, loading: float, as_of: str, meta: Meta) -> Edge:
    return Edge(subject=("symbol", symbol), predicate="loads_on", object=("factor", factor_id),
                weight=_loading(loading, f"loading of {symbol} on {factor_id}"), as_of=as_of,
                meta=meta, influence=None)


def eigen_factor_edges(model: EigenFactorModel, *, as_of: str) -> list[Edge]:
    """One edge per (symbol, structural eigenfactor). A model with no structure yields no edges."""
    stamp = _as_of(as_of)
    symbols = _symbols(model.symbols)
    common: Meta = {
        "kind": "eigen", "n_obs": float(model.n_obs), "n_assets": float(len(symbols)),
        "n_signal": float(model.n_signal), "sigma2": float(model.sigma2),
        "lambda_plus": float(model.lambda_plus), "threshold": float(model.threshold),
        **model.spec.as_meta(),
    }
    edges: list[Edge] = []
    for j in range(model.loadings.shape[1]):
        meta: Meta = {**common, "factor_rank": float(j + 1), "eigenvalue": float(model.eigenvalues[j]),
                      "variance_share": float(model.variance_share[j])}
        edges.extend(_edge(s, eigen_factor_id(j + 1), model.loadings[i, j], stamp, dict(meta))
                     for i, s in enumerate(symbols))
    return edges


def named_factor_edges(fit: FactorFit, *, as_of: str) -> list[Edge]:
    """One edge per (asset, supplied factor); ``weight`` is the OLS beta."""
    stamp = _as_of(as_of)
    assets = _symbols(fit.assets)
    edges: list[Edge] = []
    for j, name in enumerate(fit.factors):
        for i, s in enumerate(assets):
            meta: Meta = {
                "kind": "named", "estimator": "ols", "factor": name, "n_obs": float(fit.n_obs),
                "n_dropped": float(fit.n_dropped), "r2": float(fit.r2[i]),
                "t_stat": float(fit.t_betas[i, j]), "alpha": float(fit.alpha[i]),
                "resid_var": float(fit.resid_var[i]),
            }
            edges.append(_edge(s, named_factor_id(name), fit.betas[i, j], stamp, meta))
    return edges


def society_factor_edges(factor: SocietyFactor, *, run_id: str, as_of: str) -> list[Edge]:
    """One edge per symbol the society's top eigenvector covers; ``as_of`` is the SEED's ``as_of``."""
    if not run_id:
        raise ValueError("run_id is required: a society factor belongs to one simulation run")
    stamp = _as_of(as_of)
    symbols = _symbols(factor.symbols)
    meta: Meta = {
        "kind": "society", "run_id": run_id, "eigenvalue": float(factor.eigenvalue),
        "share": float(factor.share), "n_agents": float(factor.n_agents),
        "n_symbols": float(len(symbols)), "n_dropped": float(len(factor.dropped)),
    }
    return [_edge(s, society_factor_id(run_id), factor.loadings[i], stamp, dict(meta))
            for i, s in enumerate(symbols)]
