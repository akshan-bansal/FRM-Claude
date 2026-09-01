"""Measurement quality of the OSINT feed — the standard that replaces backtesting at the edge.

A feed consumed at the live decision edge cannot be backtested: there is no point-in-time history of
it, so there is no honest way to ask "what would this have earned". That does not leave us with
nothing to check. It moves the question from *forecast performance* to **instrument quality**, which
is the older and better-posed problem:

* **Reliability** — does the instrument measure *consistently*? An index that swings between two
  readings taken minutes apart, with no world event between them, is noise no matter how sensible its
  definition. Measured here as agreement between consecutive reads (test-retest), plus how often the
  feed is available and how stale it is when it answers.
* **Validity** — does it measure *what it claims*? A "market stress" index that does not co-move with
  realized market stress is not measuring stress, whatever it is called. Measured here as criterion
  correlation against an external yardstick (VIX), and as internal consistency between fields that
  should move together.

Both are computed from our own journal, so they need no vendor cooperation and improve as the record
grows. Neither is a claim about profitability — that still requires the validated strategy layer.
The purpose is narrower and more honest: to say whether this instrument is trustworthy enough to
inform a decision, and to catch the day it silently degrades.

Thin history yields wide uncertainty, so every statistic reports the sample it came from and
:class:`FeedQuality` refuses a verdict below :data:`MIN_SAMPLE`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from trading_live_claude.intel.history import DEFAULT_JOURNAL, load_journal

# Below this many reads, reliability statistics are not reported as findings.
MIN_SAMPLE = 10

# Fields that should co-move if the feed is internally coherent: (a, b, expected sign).
# These are definitional relationships, not discovered ones — conflict escalations feed the
# strategic-risk index by construction, so a zero or negative correlation is a red flag about the
# instrument rather than an interesting fact about the world.
CONSISTENCY_PAIRS: tuple[tuple[str, str, int], ...] = (
    ("conflict_events_active", "strategic_risk", +1),
    ("global_alert_count", "strategic_risk", +1),
)


@dataclass(frozen=True)
class Statistic:
    name: str
    value: float
    n: int
    note: str = ""

    @property
    def usable(self) -> bool:
        return self.n >= MIN_SAMPLE and not np.isnan(self.value)


@dataclass(frozen=True)
class FeedQuality:
    """Reliability and validity of the feed, from our own recorded observations."""

    n_reads: int
    span_hours: float
    availability: Statistic          # share of reads that came back complete
    staleness_median_h: Statistic    # typical age of the payloads behind a read
    test_retest: dict[str, Statistic]   # per-field consecutive-read agreement
    internal_consistency: dict[str, Statistic]
    criterion_validity: dict[str, Statistic]

    @property
    def has_verdict(self) -> bool:
        return self.n_reads >= MIN_SAMPLE

    def summary(self) -> str:
        if not self.has_verdict:
            return (f"feed quality: {self.n_reads} reads over {self.span_hours:.1f}h - "
                    f"below the {MIN_SAMPLE}-read minimum, no verdict yet")
        lines = [f"feed quality: {self.n_reads} reads over {self.span_hours:.1f}h",
                 f"  availability      {self.availability.value:.0%}",
                 f"  median staleness  {self.staleness_median_h.value:.1f}h"]
        if self.test_retest:
            worst = min(self.test_retest.values(), key=lambda s: s.value)
            best = max(self.test_retest.values(), key=lambda s: s.value)
            lines.append(f"  test-retest       best {best.name} {best.value:.2f} / "
                         f"worst {worst.name} {worst.value:.2f}")
        for k, s in self.internal_consistency.items():
            lines.append(f"  consistency       {k} r={s.value:+.2f} {s.note}")
        for k, s in self.criterion_validity.items():
            lines.append(f"  validity          {k} r={s.value:+.2f} {s.note}")
        return "\n".join(lines)


def _availability(path: str | Path) -> tuple[Statistic, Statistic]:
    """Share of journaled reads that were complete, and the median staleness of those reads."""
    p = Path(path)
    if not p.exists():
        return Statistic("availability", float("nan"), 0), Statistic("staleness_h", float("nan"), 0)
    total = 0
    complete = 0
    ages: list[float] = []
    for line in p.read_text(encoding="utf-8").strip().splitlines():
        if not line.strip():
            continue
        snap = json.loads(line).get("snapshot", {})
        total += 1
        if not snap.get("degraded"):
            complete += 1
        src_ages = snap.get("source_age_hours") or {}
        if src_ages:
            ages.append(float(np.median(list(src_ages.values()))))
    avail = Statistic("availability", complete / total if total else float("nan"), total)
    stale = Statistic("staleness_h", float(np.median(ages)) if ages else float("nan"), len(ages),
                      note="" if ages else "no source ages recorded yet")
    return avail, stale


def test_retest(df: pd.DataFrame) -> dict[str, Statistic]:
    """Agreement between consecutive reads, per field.

    Reported as ``1 - normalized mean absolute change``: 1.0 means consecutive reads agree exactly,
    0.0 means they differ by the field's full observed range on average. A genuinely slow-moving
    index should score high; a field that jitters between polls is measuring noise.
    """
    out: dict[str, Statistic] = {}
    for col in df.columns:
        s = df[col].dropna()
        if len(s) < 3:
            continue
        rng = float(s.max() - s.min())
        if rng <= 0:
            out[col] = Statistic(col, 1.0, len(s), note="constant across all reads")
            continue
        mac = float(s.diff().abs().mean())
        out[col] = Statistic(col, max(0.0, 1.0 - mac / rng), len(s))
    return out


def _corr(a: pd.Series, b: pd.Series) -> tuple[float, int]:
    joint = pd.concat([a, b], axis=1).dropna()
    if len(joint) < 3 or joint.iloc[:, 0].nunique() < 2 or joint.iloc[:, 1].nunique() < 2:
        return float("nan"), len(joint)
    return float(joint.iloc[:, 0].corr(joint.iloc[:, 1])), len(joint)


def internal_consistency(df: pd.DataFrame) -> dict[str, Statistic]:
    """Do fields that should move together actually do so?"""
    out: dict[str, Statistic] = {}
    for a, b, sign in CONSISTENCY_PAIRS:
        if a not in df.columns or b not in df.columns:
            continue
        r, n = _corr(df[a], df[b])
        ok = (not np.isnan(r)) and (np.sign(r) == sign or abs(r) < 0.1)
        out[f"{a}~{b}"] = Statistic(f"{a}~{b}", r, n,
                                    note="as expected" if ok else "UNEXPECTED SIGN")
    return out


def criterion_validity(df: pd.DataFrame) -> dict[str, Statistic]:
    """Does the feed's risk reading track an external yardstick it does not control?

    VIX arrives inside the same payload but is *market* data, not OSINT — the vendor reports it, it
    does not compute it. So correlating the OSINT-derived indices against VIX asks a real question:
    when this feed says the world is riskier, is the market independently agreeing?
    """
    out: dict[str, Statistic] = {}
    if "vix" not in df.columns:
        return out
    for col in ("strategic_risk", "global_alert_count", "conflict_events_active"):
        if col not in df.columns:
            continue
        r, n = _corr(df[col], df["vix"])
        out[f"{col}~vix"] = Statistic(f"{col}~vix", r, n,
                                      note="" if n >= MIN_SAMPLE else "sample too small")
    return out


def assess(path: str | Path = DEFAULT_JOURNAL) -> FeedQuality:
    """Full measurement-quality assessment of the feed from the journal."""
    df = load_journal(path, complete_only=True)
    avail, stale = _availability(path)
    span = 0.0
    if len(df) >= 2:
        span = float((df.index[-1] - df.index[0]).total_seconds() / 3600.0)
    return FeedQuality(
        n_reads=len(df), span_hours=span, availability=avail, staleness_median_h=stale,
        test_retest=test_retest(df), internal_consistency=internal_consistency(df),
        criterion_validity=criterion_validity(df),
    )
