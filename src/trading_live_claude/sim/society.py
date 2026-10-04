"""Simulated-society view -> graph edges and an influence multiplier (the "reverse adapter").

Specified in ``HOMOGENEOUS_GRAPH_SCOPE.md``. This module is pure: no I/O, no simulator, no Router.

What it does, and what it refuses to do:

* A validated :class:`SimResult` becomes ``society --holds_stance--> symbol`` edges, so the
  simulated society's reported view lives in the same journal as the intel and the fills.
* :func:`society_influence` turns one symbol's reported stance into a multiplier in ``(0, 1]``.
  **One-sided by construction**: only an adverse mean, disagreement, or herding reduce it; a
  constructive society cannot push it above 1.0. Intelligence de-risks and never levers up, the same
  property ``tests/test_intel_overlay.py::test_overlay_only_ever_reduces`` asserts of the overlay.
* :class:`AdapterParams` has NO defaults. The five numbers are a modelling decision that must be
  frozen a priori and written down before any run is scored; a default here would be a hidden choice.
  Fitting them to P&L is the outcome-feedback loop the scope excludes.
* :func:`cost_check_after_scaling` runs the round-trip-cost test on the SCALED intent. Scaling a
  position down raises its fixed-fee cost ratio, so an intent pushed under the ceiling is rejected,
  never sized back up.

Nothing here says the simulated stances were right. A stance is an LLM's reading of simulated posts.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import numpy as np

from ..execution.venue_costs import VenueCostModel
from ..intel.graph import Edge, clamp_influence, scaled_edge
from .contract import SimResult, SimSeed, Stance

# Fewer simulated agents than this and the stance matrix has no usable spectrum: with N agents the
# sample covariance has rank at most N - 1, and the top-eigenvalue share is dominated by estimation
# noise. A refusal to compute is more honest than a number.
MIN_AGENTS_FOR_SPECTRUM = 30


@dataclass(frozen=True)
class AdapterParams:
    """The five numbers of the reverse adapter. All required; see the module docstring."""

    a: float        # weight on an adverse mean stance
    b: float        # weight on disagreement (dispersion), scaled by how un-adverse the mean is
    c: float        # weight on herding (top-eigenvalue share of the stance matrix)
    d_ref: float    # dispersion at which the disagreement term is fully on
    floor: float    # smallest influence the adapter may return (> 0: never "silence" a symbol)

    def __post_init__(self) -> None:
        for name in ("a", "b", "c"):
            v = getattr(self, name)
            if not math.isfinite(v) or v < 0.0:
                raise ValueError(f"{name} must be a finite number >= 0, got {v!r}")
        if not math.isfinite(self.d_ref) or self.d_ref <= 0.0:
            raise ValueError(f"d_ref must be a finite number > 0, got {self.d_ref!r}")
        if not (math.isfinite(self.floor) and 0.0 < self.floor <= 1.0):
            raise ValueError(f"floor must be in (0, 1], got {self.floor!r}")

    def as_meta(self) -> dict[str, float | str]:
        return {"a": self.a, "b": self.b, "c": self.c, "d_ref": self.d_ref, "floor": self.floor}


def _clamp01(x: float) -> float:
    return min(1.0, max(0.0, x))


def society_influence(*, mean: float, dispersion: float, herd: float | None,
                      params: AdapterParams) -> float | None:
    """Influence in ``(0, 1]`` for one symbol, or ``None`` when the inputs carry no view.

    ``mean`` is the reported mean stance in [-1, +1] (-1 adverse); ``dispersion`` its spread across
    agents; ``herd`` the stance matrix's top-eigenvalue share, or ``None`` when it could not be
    computed (then that term is simply absent, not guessed).

        adverse = max(0, -mean)
        disperse = clamp(dispersion / d_ref, 0, 1)
        influence = clamp(1 - a*adverse - b*disperse*(1 - adverse) - c*herd, floor, 1)

    Every term is subtracted and every coefficient is >= 0, so the result cannot exceed 1.0 for any
    input; a unanimously constructive, unherded society returns exactly 1.0.
    """
    if not (math.isfinite(mean) and math.isfinite(dispersion)):
        return None
    adverse = max(0.0, -max(-1.0, min(1.0, mean)))
    disperse = _clamp01(max(0.0, dispersion) / params.d_ref)
    herd_t = 0.0 if herd is None or not math.isfinite(herd) else _clamp01(herd)
    raw = 1.0 - params.a * adverse - params.b * disperse * (1.0 - adverse) - params.c * herd_t
    return clamp_influence(max(params.floor, min(1.0, raw)))


def compose(overlay_scalar: float | None, society: float | None) -> float | None:
    """One product of the overlay scalar and the society influence, clamped to ``(0, 1]``.

    Applied once, at one seam. Both factors are written onto the ``scaled`` edge by
    :func:`society_scaled_edge`, so the composition can be audited and never double counted. A
    factor that is ``None`` carries no view and is skipped, not treated as zero.
    """
    o, s = clamp_influence(overlay_scalar), clamp_influence(society)
    if o is None:
        return s
    if s is None:
        return o
    return clamp_influence(o * s)


def top_eigenvalue_share(stances: np.ndarray, *, min_agents: int = MIN_AGENTS_FOR_SPECTRUM) -> float | None:
    """Consensus concentration: ``lambda_1 / N`` of the symbol-by-symbol correlation of agent stances.

    ``stances`` is agents x symbols. Returns a value in ``[1/N, 1]``, or ``None`` when the matrix
    cannot support it: fewer than ``min_agents`` agents, fewer than two symbols with any spread,
    non-finite entries, or a non-2-D input. Columns with no variance are dropped (a symbol every
    agent scored identically has no correlation to speak of).
    """
    x = np.asarray(stances, dtype=float)
    if x.ndim != 2 or x.shape[0] < min_agents or not np.all(np.isfinite(x)):
        return None
    keep = x.std(axis=0, ddof=1) > 1e-12
    if int(keep.sum()) < 2:
        return None
    corr = np.corrcoef(x[:, keep], rowvar=False)
    lam = np.linalg.eigvalsh((corr + corr.T) / 2.0)
    return float(lam[-1] / lam.sum()) if lam.sum() > 0 else None


def society_edges(seed: SimSeed, result: SimResult) -> list[Edge]:
    """``society --holds_stance--> symbol`` edges for a result, stamped with the SEED's ``as_of``.

    ``as_of`` comes from the snapshot the society was shown, not the wall clock, as everywhere else
    in the graph. The result must belong to the seed (a result only counts against a seed this repo
    issued). The edges carry no influence of their own: the view is reported, not yet applied.
    """
    if result.run_id != seed.run_id:
        raise ValueError(f"result {result.run_id} does not belong to seed {seed.run_id}")
    return [
        Edge(
            subject=("society", seed.run_id), predicate="holds_stance",
            object=("symbol", s.symbol), weight=float(s.mean), as_of=seed.as_of,
            meta={"dispersion": float(s.dispersion), "agents": float(s.agents),
                  "run_id": seed.run_id, "model": result.model,
                  "steps": float(result.completed_steps), "stopped_by": result.stopped_by},
        )
        for s in result.stances
    ]


def society_scaled_edge(*, seed: SimSeed, stance: Stance, params: AdapterParams,
                        overlay_scalar: float | None, herd: float | None, poll_id: str,
                        asset_class: str, intent_id: str, strategy: str,
                        reasons: Sequence[str] = ()) -> Edge | None:
    """One ``scaled`` edge whose influence is overlay x society, with both factors on the edge.

    Returns ``None`` when neither factor carries a view (nothing to say). The ``meta`` records the
    run, the two factors, the herd term and all five adapter parameters, so the sizing is
    reproducible from the record alone.
    """
    soc = society_influence(mean=stance.mean, dispersion=stance.dispersion, herd=herd, params=params)
    combined = compose(overlay_scalar, soc)
    if combined is None:
        return None
    base = scaled_edge(poll_id=poll_id, symbol=stance.symbol, scalar=combined,
                       asset_class=asset_class, intent_id=intent_id, strategy=strategy,
                       reasons=reasons, as_of=seed.as_of)
    extra: dict[str, float | str] = {
        "society_run_id": seed.run_id,
        "overlay_scalar": -1.0 if overlay_scalar is None else float(overlay_scalar),
        "society_influence": -1.0 if soc is None else float(soc),
        "herd": -1.0 if herd is None else float(herd),
    }
    extra.update({f"adapter_{k}": v for k, v in params.as_meta().items()})
    return replace(base, meta={**base.meta, **extra})


@dataclass(frozen=True)
class CostCheck:
    ok: bool
    ratio: float            # round-trip cost / notional of the SCALED intent
    reason: str             # names the number; empty when ok


def cost_check_after_scaling(*, venue: object, shares: float, price: float, influence: float | None,
                             max_ratio: float) -> CostCheck:
    """Round-trip cost test on the intent AFTER the influence has scaled it.

    ``max_ratio <= 0`` means the gate is off (the Router's default) and always passes. A scaled
    intent over the ceiling is rejected with the ratio in the reason; this never returns a larger
    size. ``shares`` is the pre-scaling size; lot flooring is the Router's concern, not this check's.
    """
    infl = clamp_influence(influence)
    scaled = shares if infl is None else shares * infl
    ratio = VenueCostModel.for_venue(venue).round_trip_cost_ratio(shares=scaled, price=price)
    if max_ratio <= 0.0 or ratio <= max_ratio:
        return CostCheck(True, ratio, "")
    return CostCheck(False, ratio,
                     f"round-trip cost {ratio:.2%} of notional > {max_ratio:.2%} cap after scaling "
                     f"by {1.0 if infl is None else infl:.3f}")


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class SocietyView:
    """What ``Router.society_view`` calls: ``symbol -> (influence, run_id)`` or ``None``.

    Built once from a validated result and its seed. It goes silent rather than stale: a view older
    than ``max_age_h`` (measured from when the run finished), or one dated in the future, answers
    ``None`` ("no view"), so an old simulation can never keep trimming orders. ``max_age_h`` has no
    default, for the same reason :class:`AdapterParams` has none. Symbols the run did not cover
    answer ``None`` too.
    """

    run_id: str
    finished_at: datetime
    influence: Mapping[str, float]
    max_age_h: float
    clock: Callable[[], datetime] = field(default=_utcnow, repr=False)

    def __post_init__(self) -> None:
        if not (math.isfinite(self.max_age_h) and self.max_age_h > 0.0):
            raise ValueError(f"max_age_h must be a finite number > 0, got {self.max_age_h!r}")

    def __call__(self, symbol: str) -> tuple[float, str] | None:
        value = clamp_influence(self.influence.get(symbol))
        if value is None:
            return None
        finished = self.finished_at if self.finished_at.tzinfo else self.finished_at.replace(tzinfo=UTC)
        age_h = (self.clock() - finished).total_seconds() / 3600.0
        if age_h < 0.0 or age_h > self.max_age_h:
            return None
        return value, self.run_id

    @classmethod
    def from_result(cls, seed: SimSeed, result: SimResult, params: AdapterParams, *,
                    max_age_h: float, min_agents: int, herd: float | None = None,
                    clock: Callable[[], datetime] = _utcnow) -> SocietyView:
        """Per-symbol influence from the reported stances through the adapter (``herd`` if known).

        ``min_agents`` has no default, for the same reason the adapter parameters have none. A
        symbol's stance counts only if at least that many simulated agents addressed it. This is the
        guard against a "society" of one: OASIS swallows model errors, so a run whose agents all
        failed still completes, and the final readout can then produce a stance from the single
        seed post (the 2026-10-03 database held exactly that: one post, by agent 0, and nothing
        else). That stance is a model reading a briefing, not a simulated society, and it must not
        size an order.
        """
        if result.run_id != seed.run_id:
            raise ValueError(f"result {result.run_id} does not belong to seed {seed.run_id}")
        if min_agents < 1:
            raise ValueError(f"min_agents must be >= 1, got {min_agents!r}")
        infl: dict[str, float] = {}
        for st in result.stances:
            if st.agents < min_agents:
                continue
            v = society_influence(mean=st.mean, dispersion=st.dispersion, herd=herd, params=params)
            if v is not None:
                infl[st.symbol] = v
        return cls(run_id=seed.run_id, finished_at=datetime.fromisoformat(result.finished_at),
                   influence=infl, max_age_h=max_age_h, clock=clock)
