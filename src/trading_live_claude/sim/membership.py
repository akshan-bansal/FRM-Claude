"""Cardholder membership and attrition: a synthetic scenario generator, and a map from real users.

Both produce the same :class:`MembershipPanel`, so a scenario and the real record can be laid side by
side and the same estimator can be run on either.

The generator (``simulate``) is a planning tool, not a forecast. Every panel it returns says
``source="synthetic"`` and carries the parameters that made it. Its parts, as asked:

* **Proportionate, active transactions.** Transactions per period are Poisson (optionally
  gamma-mixed for overdispersion) with mean ``members * tx_rate * activity``: proportional to
  membership. "Active members" are those with at least one transaction in the period.
* **Mean-reverting, cyclical activity.** ``activity = exp(x)`` with ``x = cycle + y``. The cycle is a
  sum of cosine harmonics (a weekly and a yearly one by default) at exactly the amplitudes given; ``y``
  is the gap from it, an Ornstein-Uhlenbeck (AR(1)) process pulled to zero at speed ``kappa``. Putting
  the cycle on top matters: if the level instead chased a moving cyclical target, the reversion would
  low-pass the cycle and a weekly swing would arrive at a quarter of its stated size.
* **Long-term membership and attrition.** Joins come from a base rate plus referrals from current
  members, damped by a capacity ceiling. Everyday attrition is a hazard that rises when activity
  sits below its cycle (disengagement). On top of that, rare mass-exit shocks take a fraction of
  members whose size follows a generalised Pareto law (capped), which is what makes period-to-period
  membership change left-skewed with a heavy lower tail. ``analysis/edge_tails.py`` can measure it.

The map (``panel_from_approval_db``) reads the approval store the card writes: a card is a member
(``cards.created_at``), a revocation is attrition, a signed approval (``verdict = ACCEPT``) is an
active transaction. Attrition by lapse (no signed approval for ``lapse_days``) is INFERRED, not
recorded, so the definition is a required argument and the panel says it was used.

Nothing here touches the Router, a broker or money. The store is opened read-only.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

import numpy as np
from numpy.typing import NDArray

IntArray = NDArray[np.int64]
FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class MembershipPanel:
    """One row per period. Identity: ``members_end = members_start - churned + joined``."""

    source: Literal["synthetic", "real"]
    period_days: int
    members_start: IntArray
    joined: IntArray
    churned: IntArray
    members_end: IntArray
    active_members: IntArray
    transactions: IntArray
    activity: FloatArray | None = None            # synthetic only: the latent level, exp(x)
    cycle_mean: FloatArray | None = None          # synthetic only: the cyclical mean it reverts to
    shock_loss: IntArray | None = None            # synthetic only: members lost to mass-exit shocks
    start: str | None = None                      # real only: first period's date
    params: dict[str, object] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def periods(self) -> int:
        return int(self.members_end.size)

    def describe(self) -> str:
        tag = "SYNTHETIC scenario, not measured" if self.source == "synthetic" else "REAL records"
        end = int(self.members_end[-1]) if self.periods else 0
        return (f"{tag}: {self.periods} periods of {self.period_days} day(s), members "
                f"{int(self.members_start[0]) if self.periods else 0} -> {end}, "
                f"{int(self.joined.sum())} joined, {int(self.churned.sum())} churned, "
                f"{int(self.transactions.sum())} transactions.")


# ---- the generator -----------------------------------------------------------------------------

# (period in periods, amplitude on the log scale, phase in radians)
Cycle = tuple[float, float, float]


@dataclass(frozen=True)
class ScenarioParams:
    """Scenario assumptions, not estimates. ``seed`` is required: the draw is random."""

    periods: int
    initial_members: int
    seed: int
    capacity: int = 10_000              # market ceiling: joins fade as members approach it
    join_base: float = 2.0              # joins per period regardless of members
    join_referral: float = 0.01         # joins per member per period
    tx_rate: float = 0.5                # transactions per member per period at activity 1
    kappa: float = 0.15                 # reversion speed per period, in (0, 1]
    sigma: float = 0.10                 # sd of the log-activity innovation
    cycles: tuple[Cycle, ...] = ((7.0, 0.30, 0.0), (365.0, 0.15, 0.0))
    churn_base: float = 0.01            # everyday attrition hazard per period
    churn_activity_link: float = 1.0    # how hard below-cycle activity raises the hazard
    shock_prob: float = 0.01            # chance per period of a mass-exit shock
    shock_xi: float = 0.3               # shape of the shock-size law (generalised Pareto)
    shock_scale: float = 0.03           # scale, as a fraction of members
    shock_cap: float = 0.5              # no shock removes more than this fraction
    overdispersion: float = 0.0         # gamma-mixing variance of the transaction count (0 = Poisson)

    def __post_init__(self) -> None:
        checks = [
            (self.periods >= 1, "periods must be >= 1"),
            (self.initial_members >= 0, "initial_members must be >= 0"),
            (self.capacity >= 1, "capacity must be >= 1"),
            (0.0 < self.kappa <= 1.0, "kappa must be in (0, 1]"),
            (self.sigma >= 0.0 and math.isfinite(self.sigma), "sigma must be >= 0"),
            (self.tx_rate >= 0.0, "tx_rate must be >= 0"),
            (0.0 <= self.churn_base <= 1.0, "churn_base must be in [0, 1]"),
            (0.0 <= self.shock_prob <= 1.0, "shock_prob must be in [0, 1]"),
            (0.0 < self.shock_cap <= 1.0, "shock_cap must be in (0, 1]"),
            (self.shock_scale > 0.0, "shock_scale must be > 0"),
            (self.overdispersion >= 0.0, "overdispersion must be >= 0"),
            (all(p > 1.0 for p, _, _ in self.cycles), "every cycle length must be > 1 period"),
        ]
        for ok, msg in checks:
            if not ok:
                raise ValueError(msg)


def _log_cycle(t: int, cycles: tuple[Cycle, ...]) -> float:
    return float(sum(a * math.cos(2.0 * math.pi * t / p + ph) for p, a, ph in cycles))


def simulate(p: ScenarioParams) -> MembershipPanel:
    """One synthetic scenario. Same params and seed, same panel."""
    rng = np.random.default_rng(p.seed)
    n = p.periods
    ms, jn, ch, me, am, tx, shk = (np.zeros(n, dtype=np.int64) for _ in range(7))
    act, cyc = np.zeros(n), np.zeros(n)
    m = p.initial_members
    y = 0.0                                    # gap from the cycle, mean-reverting to zero
    for t in range(n):
        logmu = _log_cycle(t, p.cycles)
        y = (1.0 - p.kappa) * y + p.sigma * float(rng.standard_normal())
        x = logmu + y
        rate = p.tx_rate * math.exp(x)
        lam = m * rate
        if p.overdispersion > 0.0 and lam > 0.0:
            lam *= float(rng.gamma(1.0 / p.overdispersion, p.overdispersion))
        t_count = int(rng.poisson(lam)) if m > 0 else 0
        a_count = int(rng.binomial(m, 1.0 - math.exp(-rate))) if m > 0 else 0
        hazard = min(0.5, p.churn_base * math.exp(-p.churn_activity_link * (x - logmu)))
        c_base = int(rng.binomial(m, hazard)) if m > 0 else 0
        lost = 0
        if m > 0 and rng.random() < p.shock_prob:
            u = float(rng.random())
            sev = (-p.shock_scale * math.log1p(-u) if abs(p.shock_xi) < 1e-9
                   else p.shock_scale * ((1.0 - u) ** (-p.shock_xi) - 1.0) / p.shock_xi)
            lost = round(min(p.shock_cap, sev) * (m - c_base))
        join_rate = max(0.0, (p.join_base + p.join_referral * m) * (1.0 - m / p.capacity))
        j = int(rng.poisson(join_rate))
        ms[t], jn[t], ch[t], tx[t], am[t], shk[t] = m, j, c_base + lost, t_count, a_count, lost
        m = m - c_base - lost + j
        me[t], act[t], cyc[t] = m, math.exp(x), math.exp(logmu)
    return MembershipPanel(source="synthetic", period_days=1, members_start=ms, joined=jn, churned=ch,
                           members_end=me, active_members=am, transactions=tx, activity=act,
                           cycle_mean=cyc, shock_loss=shk, params=asdict(p),
                           notes=("SYNTHETIC: generated from assumptions, not measured",))


# ---- the estimator ----------------------------------------------------------------------------

@dataclass(frozen=True)
class MembershipEstimates:
    tx_per_member: float              # mean transactions per member per period
    kappa: float                      # reversion speed of log activity, after removing the cycles
    sigma: float                      # sd of its innovations
    cycle_amplitudes: dict[float, float]
    churn_median: float               # median per-period attrition rate (robust to rare shocks)
    join_rate: float                  # mean joins per member per period
    periods: int
    mean_members: float


@dataclass(frozen=True)
class EstimatesRefused:
    reason: str


def estimate(panel: MembershipPanel, *, harmonic_periods: tuple[float, ...] = (7.0, 365.0),
             min_periods: int = 60, min_members: float = 30.0) -> MembershipEstimates | EstimatesRefused:
    """Method-of-moments read of a panel, synthetic or real. Refuses what it cannot support.

    log(transactions per member) is regressed on the given cosine/sine harmonics (only those that fit
    twice in the sample); the residual is the activity gap, and an AR(1) fit to it gives the
    reversion speed ``kappa = 1 - phi``. Poisson noise in low counts biases ``kappa`` upward (it looks
    like fast reversion), so a panel with few transactions per period is refused, not trusted.
    """
    n = panel.periods
    if n < min_periods:
        return EstimatesRefused(f"{n} periods; need at least {min_periods}")
    mean_members = float(panel.members_start.mean())
    if mean_members < min_members:
        return EstimatesRefused(f"mean membership {mean_members:.1f}; need at least {min_members:.0f}")
    ok = panel.members_start > 0
    r = np.where(ok, panel.transactions / np.maximum(panel.members_start, 1), np.nan)
    if int(np.sum(np.isfinite(r) & (r > 0))) < 0.9 * n:
        return EstimatesRefused("too many periods with no transactions to take a log")
    if float(panel.transactions.mean()) < 50.0:
        return EstimatesRefused("fewer than 50 transactions per period: count noise would dominate kappa")
    y = np.log(r)
    t = np.arange(n, dtype=float)
    cols = [np.ones(n)]
    used: list[float] = []
    for per in harmonic_periods:
        if per <= n / 2.0:
            cols += [np.cos(2.0 * np.pi * t / per), np.sin(2.0 * np.pi * t / per)]
            used.append(per)
    design = np.column_stack(cols)
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    u = y - design @ coef
    phi = float(np.sum(u[1:] * u[:-1]) / np.sum(u[:-1] ** 2))
    resid = u[1:] - phi * u[:-1]
    amps = {per: float(np.hypot(coef[1 + 2 * i], coef[2 + 2 * i])) for i, per in enumerate(used)}
    hazard = np.where(panel.members_start > 0, panel.churned / np.maximum(panel.members_start, 1), np.nan)
    return MembershipEstimates(
        tx_per_member=float(np.nanmean(r)), kappa=1.0 - phi, sigma=float(resid.std(ddof=1)),
        cycle_amplitudes=amps, churn_median=float(np.nanmedian(hazard)),
        join_rate=float(panel.joined.sum() / panel.members_start.sum()), periods=n,
        mean_members=mean_members)


# ---- the map from real users -------------------------------------------------------------------

def _to_date(text: str) -> date:
    s = text.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return dt.date()


def panel_from_approval_db(path: Path, *, start: date, end: date, lapse_days: int,
                           period_days: int = 1) -> MembershipPanel:
    """Map the approval store's real cards and signed approvals into a :class:`MembershipPanel`.

    * a **member** is a card, from ``cards.created_at``;
    * **attrition** is ``cards.revoked_at``, and, when ``lapse_days > 0``, also a lapse: no signed
      approval for ``lapse_days`` (counted from the last one, or from joining). A lapse is inferred, not
      recorded, and the panel's notes say it was used. ``lapse_days = 0`` uses revocations only;
    * an **active transaction** is an intent resolved ``ACCEPT`` by a card (declined and expired
      prompts are not transactions).

    The store is opened read-only. An empty store gives an all-zero panel whose notes say there was
    nothing to map, never a made-up one.
    """
    if end < start:
        raise ValueError("end is before start")
    if lapse_days < 0 or period_days < 1:
        raise ValueError("lapse_days must be >= 0 and period_days >= 1")
    if not Path(path).exists():
        raise FileNotFoundError(path)
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        cards = [(str(c), _to_date(a), _to_date(b) if b else None)
                 for c, a, b in con.execute("SELECT card_id, created_at, revoked_at FROM cards")]
        approvals: dict[str, list[date]] = {}
        for card, when in con.execute("SELECT signer_card_id, resolved_at FROM intents "
                                      "WHERE verdict = 'ACCEPT' AND signer_card_id IS NOT NULL "
                                      "AND resolved_at IS NOT NULL"):
            approvals.setdefault(str(card), []).append(_to_date(when))
    finally:
        con.close()
    n = (end - start).days // period_days + 1

    def idx(d: date) -> int:
        return (d - start).days // period_days

    joined = np.zeros(n, dtype=np.int64)
    churned = np.zeros(n, dtype=np.int64)
    tx = np.zeros(n, dtype=np.int64)
    active: list[set[str]] = [set() for _ in range(n)]
    initial = 0
    lapsed = 0
    for card, born, revoked in cards:
        acts = sorted(approvals.get(card, []))
        gone = revoked
        if lapse_days > 0:
            last = acts[-1] if acts else born
            lapse_at = last + timedelta(days=lapse_days)
            if lapse_at <= end and (gone is None or lapse_at < gone):
                gone, lapsed = lapse_at, lapsed + 1
        if gone is not None and gone < start:
            continue                                   # left before the window opened
        if born < start:
            initial += 1
        elif born <= end:
            joined[idx(born)] += 1
        if gone is not None and start <= gone <= end:
            churned[idx(gone)] += 1
        for d in acts:
            if start <= d <= end:
                tx[idx(d)] += 1
                active[idx(d)].add(card)
    ms = np.zeros(n, dtype=np.int64)
    me = np.zeros(n, dtype=np.int64)
    m = initial
    for t in range(n):
        ms[t] = m
        m = m - int(churned[t]) + int(joined[t])
        me[t] = m
    notes = [f"REAL records: {len(cards)} card(s) and {sum(len(v) for v in approvals.values())} signed "
             f"approval(s) in the store"]
    if not cards:
        notes.append("nothing to map: the store holds no cards")
    if lapse_days > 0:
        notes.append(f"attrition includes {lapsed} INFERRED lapse(s) (no signed approval for "
                     f"{lapse_days} day(s)); only revocations are recorded facts")
    if len(cards) < 30:
        notes.append(f"{len(cards)} card(s): these are counts of a handful of devices, not rates")
    return MembershipPanel(source="real", period_days=period_days, members_start=ms, joined=joined,
                           churned=churned, members_end=me,
                           active_members=np.array([len(s) for s in active], dtype=np.int64),
                           transactions=tx, start=start.isoformat(),
                           params={"lapse_days": lapse_days, "period_days": period_days},
                           notes=tuple(notes))
