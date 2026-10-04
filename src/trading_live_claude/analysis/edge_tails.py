"""Edge tails: skew and extreme-value (peaks-over-threshold) fits for the graph's edge weights.

Contract, same as the rest of ``analysis``: this is a ruler. It describes the shape of the numbers the
graph journal holds (the ``weight`` of an edge family, or its ``influence``). It decides nothing,
and nothing in execution, risk, brokers, strategies, monitor or the daemon imports it
(``tests/test_edge_tails.py`` enforces that).

What it measures, per edge family and per tail:

* **Skew** of the whole sample (Fisher-Pearson). Left-skewed (negative) means a long tail toward low
  values: most of an ``influence`` sits near 1 with a few hard de-risks far below it.
* **A generalised Pareto fit to the exceedances** over a high quantile (peaks over threshold), for the
  lower tail (by negating) and the upper tail. The shape ``xi`` says how the tail ends: ``xi > 0`` a
  heavy tail with no finite endpoint, ``xi = 0`` exponential, ``xi < 0`` a bounded tail with a finite
  end. The scale, a bootstrap interval for ``xi``, a parametric-bootstrap goodness-of-fit p-value,
  and a return level ("the value exceeded about once per m edges") come with it.
* **Threshold stability**: ``xi`` at several quantiles. An estimate that moves a lot with the
  threshold is not a tail estimate yet.

Honesty rules:

* It refuses rather than guesses: too few exceedances, or many identical values among them. Identical
  values are the signature of mass sitting at a boundary (an influence clamped to its floor or to 1.0,
  integer counts), which is a different thing from a tail, and fitting a continuous tail to it
  would manufacture a shape.
* The estimator is probability-weighted moments (Hosking and Wallis 1987): closed form, NumPy only,
  and stable at the sample sizes an edge family has. It is less efficient than maximum likelihood
  for heavy tails and valid for ``xi < 1``; a fitted ``xi`` at or above 0.9 is refused as unreliable.
* Randomness (the bootstrap) takes a REQUIRED ``seed``, so a result can be reproduced.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from ..intel.graph import Edge

FloatArray = NDArray[np.float64]
Side = Literal["lower", "upper"]

MIN_EXCEEDANCES = 30        # the usual floor for a peaks-over-threshold fit
MAX_TIE_FRACTION = 0.20     # more repeats than this among exceedances means a boundary, not a tail
XI_LIMIT = 0.9              # PWM is valid for xi < 1; refuse near it
DEFAULT_Q = 0.90
STABILITY_QS = (0.80, 0.85, 0.90, 0.95)


@dataclass(frozen=True)
class TailFit:
    side: Side
    n: int                          # sample size
    q: float                        # threshold quantile
    threshold: float                # in the data's own units
    n_exceed: int
    xi: float                       # GPD shape
    sigma: float                    # GPD scale
    xi_ci: tuple[float, float]      # bootstrap percentile interval for xi
    gof_p: float                    # parametric-bootstrap KS p-value (small = the GPD fits poorly)
    shape: str                      # "heavy", "exponential-like" or "bounded"

    def return_level(self, m: float) -> float:
        """The value exceeded about once per ``m`` observations (in the data's own units)."""
        rate = self.n_exceed / self.n
        if m * rate <= 1.0:
            raise ValueError(f"m={m} is inside the sample's own range; need m > {1.0 / rate:.1f}")
        t = m * rate
        y = self.sigma * math.log(t) if abs(self.xi) < 1e-9 else self.sigma / self.xi * (t ** self.xi - 1.0)
        return self.threshold + y if self.side == "upper" else self.threshold - y


@dataclass(frozen=True)
class TailRefused:
    side: Side
    n: int
    reason: str


TailResult = TailFit | TailRefused


@dataclass(frozen=True)
class EdgeTailReport:
    predicate: str
    measure: str                    # "weight" or "influence"
    n: int
    skew: float | None
    lower: TailResult | None
    upper: TailResult | None
    xi_stability: dict[str, list[tuple[float, int, float]]] = field(default_factory=dict)


# ---- numbers -----------------------------------------------------------------------------------

def skewness(x: Sequence[float] | FloatArray) -> float | None:
    """Sample (Fisher-Pearson, bias-adjusted) skewness; ``None`` below 8 points or with no spread."""
    a = np.asarray(x, dtype=float)
    a = a[np.isfinite(a)]
    n = a.size
    if n < 8:
        return None
    s = a.std(ddof=1)
    if s <= 0.0:
        return None
    return float(n / ((n - 1) * (n - 2)) * np.sum(((a - a.mean()) / s) ** 3))


def _pwm(exc: FloatArray) -> tuple[float, float]:
    """(xi, sigma) of a generalised Pareto by probability-weighted moments."""
    y = np.sort(exc)
    n = y.size
    a0 = float(y.mean())
    a1 = float(np.sum(y * (n - np.arange(1, n + 1)) / (n - 1)) / n)
    if a1 <= 0.0 or a0 <= 0.0:
        return float("nan"), float("nan")
    t = a0 / a1
    xi = (t - 4.0) / (t - 2.0) if abs(t - 2.0) > 1e-12 else float("nan")
    return xi, a0 * (1.0 - xi)


def _cdf(y: FloatArray, xi: float, sigma: float) -> FloatArray:
    if abs(xi) < 1e-9:
        return np.asarray(1.0 - np.exp(-y / sigma))
    z = np.maximum(1.0 + xi * y / sigma, 0.0)
    out = 1.0 - np.where(z > 0, z, 0.0) ** (-1.0 / xi)
    return np.asarray(np.where(z > 0, out, 1.0))


def _draw(rng: np.random.Generator, n: int, xi: float, sigma: float) -> FloatArray:
    u = rng.random(n)
    if abs(xi) < 1e-9:
        return np.asarray(-sigma * np.log1p(-u))
    return np.asarray(sigma * ((1.0 - u) ** (-xi) - 1.0) / xi)


def _ks(exc: FloatArray, xi: float, sigma: float) -> float:
    y = np.sort(exc)
    n = y.size
    f = _cdf(y, xi, sigma)
    i = np.arange(1, n + 1)
    return float(max(np.max(i / n - f), np.max(f - (i - 1) / n)))


def _label(xi: float) -> str:
    return "heavy" if xi > 0.1 else "bounded" if xi < -0.1 else "exponential-like"


# ---- the fit -----------------------------------------------------------------------------------

def fit_tail(x: Sequence[float] | FloatArray, side: Side, *, seed: int, q: float = DEFAULT_Q,
             min_exceedances: int = MIN_EXCEEDANCES, n_boot: int = 200) -> TailResult:
    """Peaks-over-threshold fit of one tail. ``seed`` is required (the bootstrap is random).

    The lower tail is fitted by negating, so every figure of ``xi`` reads the same way on both sides:
    positive is heavy, negative is bounded. ``threshold`` and ``return_level`` are returned in the
    data's own units.
    """
    a = np.asarray(x, dtype=float)
    a = a[np.isfinite(a)]
    n = int(a.size)
    if not 0.5 <= q < 1.0:
        raise ValueError(f"q must be in [0.5, 1), got {q!r}")
    if n == 0:
        return TailRefused(side, 0, "no values")
    v = a if side == "upper" else -a
    u = float(np.quantile(v, q))
    exc = v[v > u] - u
    if exc.size < min_exceedances:
        return TailRefused(side, n, f"only {exc.size} exceedances over the {q:.0%} quantile; "
                                    f"need at least {min_exceedances}")
    ties = 1.0 - np.unique(exc).size / exc.size
    if ties > MAX_TIE_FRACTION:
        return TailRefused(side, n, f"{ties:.0%} of the exceedances repeat an earlier value: that is mass "
                                    "at a boundary or integer counts, not a continuous tail")
    xi, sigma = _pwm(exc)
    if not (math.isfinite(xi) and math.isfinite(sigma)) or sigma <= 0.0:
        return TailRefused(side, n, "the moment estimator did not converge on these exceedances")
    if xi >= XI_LIMIT:
        return TailRefused(side, n, f"fitted shape {xi:.2f} is too close to 1 for a moment estimator")
    rng = np.random.default_rng(seed)
    boots: list[float] = []
    for _ in range(n_boot):
        b_xi, _s = _pwm(rng.choice(exc, size=exc.size, replace=True))
        if math.isfinite(b_xi):
            boots.append(b_xi)
    ci = (float(np.percentile(boots, 5)), float(np.percentile(boots, 95))) if boots else (xi, xi)
    d_obs = _ks(exc, xi, sigma)
    worse = 0
    for _ in range(n_boot):
        sim = _draw(rng, exc.size, xi, sigma)
        s_xi, s_sigma = _pwm(sim)
        if math.isfinite(s_xi) and s_sigma > 0 and _ks(sim, s_xi, s_sigma) >= d_obs:
            worse += 1
    thr = u if side == "upper" else -u
    return TailFit(side=side, n=n, q=q, threshold=thr, n_exceed=int(exc.size), xi=float(xi),
                   sigma=float(sigma), xi_ci=ci, gof_p=(worse + 1) / (n_boot + 1), shape=_label(xi))


def xi_stability(x: Sequence[float] | FloatArray, side: Side, *, seed: int,
                 qs: Sequence[float] = STABILITY_QS,
                 min_exceedances: int = MIN_EXCEEDANCES) -> list[tuple[float, int, float]]:
    """(q, exceedances, xi) at several thresholds; thresholds that cannot be fitted are left out."""
    out: list[tuple[float, int, float]] = []
    for q in qs:
        r = fit_tail(x, side, seed=seed, q=q, min_exceedances=min_exceedances, n_boot=20)
        if isinstance(r, TailFit):
            out.append((q, r.n_exceed, r.xi))
    return out


# ---- edges -------------------------------------------------------------------------------------

def edge_values(edges: Sequence[Edge], predicate: str, *, field_name: Literal["weight", "influence"]
                = "weight") -> FloatArray:
    """The finite ``weight`` (or ``influence``, skipping edges that carry none) of one predicate."""
    vals = [(e.weight if field_name == "weight" else e.influence) for e in edges if e.predicate == predicate]
    arr = np.array([v for v in vals if v is not None], dtype=float)
    return arr[np.isfinite(arr)]


def tail_report(edges: Sequence[Edge], predicate: str, *, seed: int,
                field_name: Literal["weight", "influence"] = "weight", q: float = DEFAULT_Q,
                min_exceedances: int = MIN_EXCEEDANCES, n_boot: int = 200) -> EdgeTailReport:
    """Skew and both tails for one edge family. Always returns a report; refusals are inside it."""
    x = edge_values(edges, predicate, field_name=field_name)
    lower = fit_tail(x, "lower", seed=seed, q=q, min_exceedances=min_exceedances, n_boot=n_boot)
    upper = fit_tail(x, "upper", seed=seed, q=q, min_exceedances=min_exceedances, n_boot=n_boot)
    stab = {s: xi_stability(x, s, seed=seed, min_exceedances=min_exceedances) for s in ("lower", "upper")}
    return EdgeTailReport(predicate=predicate, measure=field_name, n=int(x.size), skew=skewness(x),
                          lower=lower, upper=upper, xi_stability=stab)


def describe(r: EdgeTailReport) -> str:
    """One paragraph, in words, of what the report does and does not say."""
    sk = "too few points to measure skew" if r.skew is None else (
        f"skew {r.skew:+.2f} ({'left-skewed' if r.skew < -0.5 else 'right-skewed' if r.skew > 0.5 else 'roughly symmetric'})")
    parts = [f"{r.predicate}.{r.measure}: n={r.n}, {sk}."]
    for name, t in (("lower tail", r.lower), ("upper tail", r.upper)):
        if t is None:
            continue
        if isinstance(t, TailRefused):
            parts.append(f"{name}: not fitted ({t.reason}).")
        else:
            parts.append(f"{name}: {t.shape} (xi {t.xi:+.2f}, 90% interval {t.xi_ci[0]:+.2f} to {t.xi_ci[1]:+.2f}, "
                         f"{t.n_exceed} exceedances, fit p={t.gof_p:.2f}).")
    return " ".join(parts)
