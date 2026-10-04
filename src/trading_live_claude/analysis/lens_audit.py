"""Lens audit: does a feature -> outcome relationship survive a held-out read and a shuffled null?

Two attention-weighted read-outs, ported from the quiz-ladder study that prompted them:

* **reliability** - predict each event's outcome from events in *other* groups (other symbols, or
  other periods), weighting those neighbours by feature similarity. Answers: does the relationship
  transfer, or is it a property of one symbol / one regime?
* **future** - predict each event's outcome from the same group's *earlier, already-matured* events.
  Answers: does history, read through feature similarity, say anything about what comes next?

Each lens reports a Spearman rho, a permutation p-value (outcomes shuffled within group), and the
MAE of the attention read against a uniform read over the *same* admissible neighbours, so the test
isolates whether similarity weighting adds anything beyond averaging the neighbours. Results come
pooled (one verdict for all events) or per group (one verdict per symbol, from a single attention
matrix, each with its own permutation test).

Contract, same as the rest of ``analysis``: this is a ruler. Outcomes are forward-looking and are
used only as targets, never as features, and nothing here is imported by execution, risk, brokers,
strategies, monitor or the daemon. ``tests/test_lens_audit.py`` enforces the import boundary.

Label maturity is the one thing that must not be skipped. A forward return over ``h`` bars is not
known until ``h`` bars after its event, so a past event is a legal neighbour for the future lens
only if ``time_i - time_j >= label_horizon``. Without that, overlapping windows manufacture skill.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]
BoolArray = NDArray[np.bool_]

DEFAULT_TAU = 0.15
DEFAULT_RECENCY = 0.05
DEFAULT_ALPHA = 0.05
DEFAULT_PERMUTATIONS = 2000
MIN_EVENTS = 10
MAX_EVENTS = 3000  # the n x n attention matrix is dense


@dataclass(frozen=True)
class LensResult:
    """One lens, scored. ``supported`` needs p < alpha, rho > 0 and a lower MAE than the baseline."""

    lens: str
    n_scored: int
    n_without_neighbours: int
    spearman: float
    p_value: float
    mae: float
    mae_baseline: float
    supported: bool


@dataclass(frozen=True)
class LensAudit:
    reliability: LensResult
    future: LensResult | None


# --------------------------------------------------------------------------- inputs


def _prepare(
    features: object,
    outcome: object,
    groups: object,
    time: object | None,
    *,
    max_events: int,
) -> tuple[FloatArray, FloatArray, IntArray, FloatArray | None, list[object]]:
    """Validate, drop unknown outcomes, z-score the features. Labels are never used in scaling."""
    x = np.asarray(features, dtype=float)
    y = np.asarray(outcome, dtype=float)
    if x.ndim != 2:
        raise ValueError(f"features must be 2-D (events x features); got shape {x.shape}")
    n = len(x)
    if y.shape != (n,):
        raise ValueError(f"outcome must have one value per event ({n}); got shape {y.shape}")
    g_raw = np.asarray(groups)
    if g_raw.shape != (n,):
        raise ValueError(f"groups must have one value per event ({n}); got shape {g_raw.shape}")
    t: FloatArray | None = None
    if time is not None:
        t = np.asarray(time, dtype=float)
        if t.shape != (n,) or not np.isfinite(t).all():
            raise ValueError("time must be finite with one value per event")
    if not np.isfinite(x).all():
        raise ValueError("features contain NaN/inf; drop or impute them before auditing")

    keep = np.isfinite(y)  # unknown outcome (e.g. window runs off the end) is excluded, never 0
    x, y, g_raw = x[keep], y[keep], g_raw[keep]
    if t is not None:
        t = t[keep]
    if len(y) < MIN_EVENTS:
        raise ValueError(f"need at least {MIN_EVENTS} events with a known outcome; got {len(y)}")
    if len(y) > max_events:
        raise ValueError(
            f"{len(y)} events exceeds max_events={max_events}; subsample or aggregate first "
            "(the attention matrix is dense n x n)")

    uniq, g = np.unique(g_raw, return_inverse=True)
    sd = x.std(axis=0)
    z = (x - x.mean(axis=0)) / np.where(sd < 1e-12, 1.0, sd)
    return z, y, g.astype(np.int64), t, list(uniq.tolist())


# --------------------------------------------------------------------------- statistics


def _rank(a: FloatArray) -> FloatArray:
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a))
    sa = a[order]
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and sa[j + 1] == sa[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2
        i = j + 1
    return ranks


def _spearman(a: FloatArray, b: FloatArray) -> float:
    if len(a) < 3 or np.ptp(a) == 0 or np.ptp(b) == 0:
        return float("nan")
    return float(np.corrcoef(_rank(a), _rank(b))[0, 1])


def _attention(z: FloatArray, allowed: BoolArray, tau: float, bias: FloatArray | None) -> FloatArray:
    """Cosine-similarity attention. Disallowed pairs get zero weight; empty rows are all zero."""
    zn = z / (np.linalg.norm(z, axis=1, keepdims=True) + 1e-9)
    logits = (zn @ zn.T) / tau
    if bias is not None:
        logits = logits + bias
    logits = np.where(allowed, logits, -np.inf)
    any_ok = allowed.any(axis=1, keepdims=True)
    peak = np.where(any_ok, logits.max(axis=1, keepdims=True), 0.0)
    e = np.where(allowed, np.exp(logits - peak), 0.0)
    total = e.sum(axis=1, keepdims=True)
    weights: FloatArray = np.divide(e, total, out=np.zeros_like(e), where=total > 0)
    return weights


def _score_sets(
    lens: str,
    a: FloatArray,
    allowed: BoolArray,
    y: FloatArray,
    g: IntArray,
    members: Mapping[object, BoolArray],
    *,
    min_scored: int,
    alpha: float,
    n_perm: int,
    seed: int,
) -> dict[object, LensResult]:
    """Score one or more event sets against a single attention matrix.

    Each set gets its own rho, MAE and permutation p-value. The permutation loop is shared: outcomes
    are shuffled within group once per draw and every set is evaluated on that draw. Sets with fewer
    than ``min_scored`` scoreable events are left out rather than reported on a handful of points.
    """
    has_nb = allowed.any(axis=1)
    pred = a @ y
    uniform = (allowed * y[None, :]).sum(axis=1) / np.maximum(allowed.sum(axis=1), 1)

    live: dict[object, tuple[BoolArray, float, float, float, int]] = {}
    for key, member in members.items():
        sel = member & has_nb
        if int(sel.sum()) < min_scored:
            continue
        live[key] = (
            sel,
            _spearman(pred[sel], y[sel]),
            float(np.abs(pred - y)[sel].mean()),
            float(np.abs(uniform - y)[sel].mean()),
            int((member & ~has_nb).sum()),
        )

    rng = np.random.default_rng(seed)
    blocks = [np.flatnonzero(g == k) for k in np.unique(g)]
    exceed = dict.fromkeys(live, 0)
    for _ in range(n_perm):
        y_perm = y.copy()
        for idx in blocks:
            y_perm[idx] = y[rng.permutation(idx)]
        pp = a @ y_perm
        for key, (sel, obs, _mae, _base, _nb) in live.items():
            stat = _spearman(pp[sel], y[sel])
            if np.isfinite(obs) and np.isfinite(stat) and abs(stat) >= abs(obs):
                exceed[key] += 1

    out: dict[object, LensResult] = {}
    for key, (sel, obs, mae, base, n_without) in live.items():
        p = 1.0 if not np.isfinite(obs) else (1 + exceed[key]) / (n_perm + 1)
        out[key] = LensResult(
            lens=lens,
            n_scored=int(sel.sum()),
            n_without_neighbours=n_without,
            spearman=obs,
            p_value=float(p),
            mae=mae,
            mae_baseline=base,
            supported=bool(np.isfinite(obs) and p < alpha and obs > 0 and mae < base),
        )
    return out


def _pooled(
    lens: str, a: FloatArray, allowed: BoolArray, y: FloatArray, g: IntArray,
    *, alpha: float, n_perm: int, seed: int,
) -> LensResult:
    everyone = np.ones(len(y), dtype=bool)
    got = _score_sets(lens, a, allowed, y, g, {"all": everyone},
                      min_scored=3, alpha=alpha, n_perm=n_perm, seed=seed)
    if "all" not in got:
        raise ValueError(f"{lens}: fewer than 3 events have an admissible neighbour; "
                         "widen the groups or shorten label_horizon")
    return got["all"]


def _by_group(
    lens: str, a: FloatArray, allowed: BoolArray, y: FloatArray, g: IntArray, labels: list[object],
    *, min_scored: int, alpha: float, n_perm: int, seed: int,
) -> dict[object, LensResult]:
    members = {label: g == k for k, label in enumerate(labels)}
    return _score_sets(lens, a, allowed, y, g, members,
                       min_scored=min_scored, alpha=alpha, n_perm=n_perm, seed=seed)


# --------------------------------------------------------------------------- masks


def _reliability_mask(g: IntArray, t: FloatArray | None, label_horizon: float) -> BoolArray:
    """Neighbours from other groups; with ``time``, also not within one label window of each other."""
    allowed: BoolArray = g[:, None] != g[None, :]
    if t is not None and label_horizon > 0:
        allowed &= np.abs(t[:, None] - t[None, :]) >= label_horizon
    return allowed


def _future_mask(g: IntArray, t: FloatArray, label_horizon: float) -> BoolArray:
    """Same group, strictly earlier, and the neighbour's label already matured at time ``t_i``."""
    same: BoolArray = g[:, None] == g[None, :]
    matured: BoolArray = (t[:, None] - t[None, :]) >= max(label_horizon, 1e-12)
    return same & matured


def _rank_gap(g: IntArray, t: FloatArray) -> FloatArray:
    """Within-group event-rank distance, for the recency bias."""
    rank = np.zeros(len(g))
    for k in np.unique(g):
        idx = np.flatnonzero(g == k)
        rank[idx[np.argsort(t[idx], kind="mergesort")]] = np.arange(len(idx))
    return rank[:, None] - rank[None, :]


# --------------------------------------------------------------------------- set-up per lens


def _reliability_setup(
    features: object, outcome: object, groups: object, *,
    time: object | None, label_horizon: float, tau: float, max_events: int,
) -> tuple[FloatArray, BoolArray, FloatArray, IntArray, list[object]]:
    z, y, g, t, labels = _prepare(features, outcome, groups, time, max_events=max_events)
    if len(labels) < 2:
        raise ValueError("reliability needs at least two groups")
    allowed = _reliability_mask(g, t, label_horizon)
    return _attention(z, allowed, tau, None), allowed, y, g, labels


def _future_setup(
    features: object, outcome: object, groups: object, *,
    time: object, label_horizon: float, tau: float, recency: float, max_events: int,
) -> tuple[FloatArray, BoolArray, FloatArray, IntArray, list[object]]:
    if label_horizon <= 0:
        raise ValueError("label_horizon must be positive: a label is unknown until its window closes")
    z, y, g, t, labels = _prepare(features, outcome, groups, time, max_events=max_events)
    if t is None:  # pragma: no cover - time is a required argument
        raise ValueError("time is required")
    allowed = _future_mask(g, t, label_horizon)
    bias = -recency * np.where(allowed, _rank_gap(g, t), 0.0)
    return _attention(z, allowed, tau, bias), allowed, y, g, labels


# --------------------------------------------------------------------------- public lenses


def reliability_lens(
    features: object,
    outcome: object,
    groups: object,
    *,
    time: object | None = None,
    label_horizon: float = 0.0,
    tau: float = DEFAULT_TAU,
    alpha: float = DEFAULT_ALPHA,
    n_perm: int = DEFAULT_PERMUTATIONS,
    seed: int = 7,
    max_events: int = MAX_EVENTS,
) -> LensResult:
    """Leave-one-group-out: read each event from other groups' events, weighted by similarity.

    Pass ``time`` and ``label_horizon`` when groups are symbols observed over the same dates:
    neighbours whose label windows overlap the event's are then excluded, which removes the shared
    market move that would otherwise make cross-symbol transfer look better than it is.
    """
    a, allowed, y, g, _ = _reliability_setup(
        features, outcome, groups, time=time, label_horizon=label_horizon, tau=tau,
        max_events=max_events)
    return _pooled("reliability", a, allowed, y, g, alpha=alpha, n_perm=n_perm, seed=seed)


def reliability_by_group(
    features: object,
    outcome: object,
    groups: object,
    *,
    time: object | None = None,
    label_horizon: float = 0.0,
    tau: float = DEFAULT_TAU,
    alpha: float = DEFAULT_ALPHA,
    n_perm: int = DEFAULT_PERMUTATIONS,
    seed: int = 7,
    max_events: int = MAX_EVENTS,
    min_scored: int = MIN_EVENTS,
) -> dict[object, LensResult]:
    """One reliability verdict per group, each read only from the other groups.

    Groups with fewer than ``min_scored`` scoreable events are omitted, not reported.
    """
    a, allowed, y, g, labels = _reliability_setup(
        features, outcome, groups, time=time, label_horizon=label_horizon, tau=tau,
        max_events=max_events)
    return _by_group("reliability", a, allowed, y, g, labels,
                     min_scored=min_scored, alpha=alpha, n_perm=n_perm, seed=seed)


def future_lens(
    features: object,
    outcome: object,
    groups: object,
    *,
    time: object,
    label_horizon: float,
    tau: float = DEFAULT_TAU,
    recency: float = DEFAULT_RECENCY,
    alpha: float = DEFAULT_ALPHA,
    n_perm: int = DEFAULT_PERMUTATIONS,
    seed: int = 7,
    max_events: int = MAX_EVENTS,
) -> LensResult:
    """Causal read: each event from its own group's earlier events whose labels had matured.

    ``label_horizon`` is required, not defaulted. Set it to the forward-return horizon in the same
    units as ``time`` (bars). Using a smaller value lets overlapping windows leak into the read.
    """
    a, allowed, y, g, _ = _future_setup(
        features, outcome, groups, time=time, label_horizon=label_horizon, tau=tau,
        recency=recency, max_events=max_events)
    return _pooled("future", a, allowed, y, g, alpha=alpha, n_perm=n_perm, seed=seed)


def future_by_group(
    features: object,
    outcome: object,
    groups: object,
    *,
    time: object,
    label_horizon: float,
    tau: float = DEFAULT_TAU,
    recency: float = DEFAULT_RECENCY,
    alpha: float = DEFAULT_ALPHA,
    n_perm: int = DEFAULT_PERMUTATIONS,
    seed: int = 7,
    max_events: int = MAX_EVENTS,
    min_scored: int = MIN_EVENTS,
) -> dict[object, LensResult]:
    """One future verdict per group; groups with too few scoreable events are omitted."""
    a, allowed, y, g, labels = _future_setup(
        features, outcome, groups, time=time, label_horizon=label_horizon, tau=tau,
        recency=recency, max_events=max_events)
    return _by_group("future", a, allowed, y, g, labels,
                     min_scored=min_scored, alpha=alpha, n_perm=n_perm, seed=seed)


def audit_lenses(
    features: object,
    outcome: object,
    groups: object,
    *,
    time: object,
    label_horizon: float,
    tau: float = DEFAULT_TAU,
    recency: float = DEFAULT_RECENCY,
    alpha: float = DEFAULT_ALPHA,
    n_perm: int = DEFAULT_PERMUTATIONS,
    seed: int = 7,
    max_events: int = MAX_EVENTS,
) -> LensAudit:
    """Run both lenses on the same events. ``time`` and ``label_horizon`` apply to both."""
    return LensAudit(
        reliability=reliability_lens(
            features, outcome, groups, time=time, label_horizon=label_horizon, tau=tau,
            alpha=alpha, n_perm=n_perm, seed=seed, max_events=max_events),
        future=future_lens(
            features, outcome, groups, time=time, label_horizon=label_horizon, tau=tau,
            recency=recency, alpha=alpha, n_perm=n_perm, seed=seed, max_events=max_events),
    )


def render_markdown(audit: LensAudit) -> str:
    """One table, one verdict word per lens. 'not distinguishable' is a finding, not a failure."""
    rows = [audit.reliability] + ([audit.future] if audit.future is not None else [])
    lines = [
        "| lens | events | rho | p | MAE | uniform-read MAE | verdict |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        verdict = "supported" if r.supported else "not distinguishable from chance"
        lines.append(
            f"| {r.lens} | {r.n_scored} | {r.spearman:+.3f} | {r.p_value:.4f} | "
            f"{r.mae:.5f} | {r.mae_baseline:.5f} | {verdict} |")
    return "\n".join(lines)
