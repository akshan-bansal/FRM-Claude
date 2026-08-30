"""Event-intelligence features from the WorldMonitor intel archive.

The archive (``get_intel_timeline`` / ``search_intel_history``) returns dated records carrying both
``occurredAt`` and ``ingestedAt`` (epoch milliseconds). Retention is short — roughly a month, with
sparse record counts — so this is **not** a backtestable history: it cannot support a multi-year
walk-forward, and no claim of validated alpha is made from it. What it *does* support is genuine
near-term situational intelligence: how intense event flow is **right now** relative to its own
recent baseline, and which way it is moving.

That is the value taken here. :func:`event_intensity` buckets records by ``ingestedAt`` (when the
information became knowable — the point-in-time-correct field, so the recent-vs-baseline comparison
is not contaminated by late-arriving backfill) and reports, per domain:

* **rate** — events per day in the recent window,
* **baseline** — events per day over the remaining archive,
* **acceleration** — recent rate divided by baseline (>1 means flow is picking up).

Acceleration is the useful part: a raw count says little without knowing what normal looks like for
that domain. It feeds the live overlay as an additional *de-risk-only* gate.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

INTEL_DOMAINS: tuple[str, ...] = ("conflict", "military", "energy")


@dataclass(frozen=True)
class EventIntensity:
    domain: str
    recent_count: int
    recent_rate: float       # events/day in the recent window
    baseline_rate: float     # events/day over the rest of the archive
    acceleration: float      # recent_rate / baseline_rate (1.0 = normal, >1 = accelerating)
    span_days: float         # how much archive we actually got (honesty about depth)
    countries: dict[str, int] = field(default_factory=dict)


def _ms_to_dt(v: Any) -> datetime | None:
    """Archive timestamps are epoch milliseconds. Anything else is unusable."""
    if isinstance(v, (int, float)) and v > 0:
        try:
            return datetime.fromtimestamp(float(v) / 1000.0, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def event_intensity(records: Sequence[dict[str, Any]], *, domain: str,
                    recent_days: float = 3.0, now: datetime | None = None) -> EventIntensity:
    """Recent-vs-baseline event flow for one domain, bucketed by ``ingestedAt``.

    ``recent_rate`` covers the last ``recent_days``; ``baseline_rate`` covers everything older in the
    archive. With no older records the baseline falls back to the recent rate, giving
    ``acceleration = 1.0`` — i.e. "no evidence of a pickup" rather than a fabricated spike.
    """
    now = now or datetime.now(UTC)
    stamps: list[datetime] = []
    countries: dict[str, int] = {}
    for r in records:
        # ingestedAt is when WorldMonitor knew it; fall back to occurredAt when absent.
        ts = _ms_to_dt(r.get("ingestedAt")) or _ms_to_dt(r.get("occurredAt"))
        if ts is None:
            continue
        stamps.append(ts)
        cc = str(r.get("country") or "").upper()
        if cc:
            countries[cc] = countries.get(cc, 0) + 1

    if not stamps:
        return EventIntensity(domain=domain, recent_count=0, recent_rate=0.0, baseline_rate=0.0,
                              acceleration=1.0, span_days=0.0)

    cutoff = now - timedelta(days=recent_days)
    recent = [t for t in stamps if t >= cutoff]
    older = [t for t in stamps if t < cutoff]
    span = (max(stamps) - min(stamps)).total_seconds() / 86400.0

    recent_rate = len(recent) / recent_days
    if older:
        older_span = max((cutoff - min(older)).total_seconds() / 86400.0, 1.0)
        baseline_rate = len(older) / older_span
    else:
        baseline_rate = recent_rate      # no older data -> assume normal, never a fake spike
    accel = recent_rate / baseline_rate if baseline_rate > 0 else 1.0

    return EventIntensity(domain=domain, recent_count=len(recent), recent_rate=recent_rate,
                          baseline_rate=baseline_rate, acceleration=accel, span_days=span,
                          countries=dict(sorted(countries.items(), key=lambda kv: -kv[1])[:10]))
