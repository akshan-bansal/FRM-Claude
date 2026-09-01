"""Read the intel journal back as a time series, and derive features from our own history.

Every overlay read is appended to ``state/intel_overlay.jsonl``. Until now that journal was
write-only: the live path consumed one snapshot and discarded everything it had learned, so the
overlay could not distinguish a one-off spike from a condition that had been building for days.

This module closes that loop. It loads the journal into a time-indexed frame and derives the three
things a single snapshot cannot express:

* **change** — the level *and* its direction. A strategic-risk index of 65 that rose 12 points in a
  day means something different from a 65 that is subsiding.
* **relative position** — where a reading sits against *its own* recorded history, so "elevated" is
  measured against what this feed actually does rather than a hand-picked constant.
* **persistence** — how many consecutive reads a condition has held. This is the direct answer to the
  sparse-archive artifact problem: a 6x event acceleration seen once is noise, the same reading
  across five consecutive polls is a regime.

**Why these are honest.** They are computed from values *we* recorded at the moment we observed them,
so they are point-in-time correct by construction — unlike the vendor's live-only snapshot, which has
no queryable history at all. As the journal accrues, these become the first genuinely backtestable
OSINT features in this project. Until it does, every derivation degrades to a neutral value rather
than inventing precision from three data points; ``MIN_HISTORY`` sets that bar.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from trading_live_claude.intel.overlay import IntelSnapshot, OverlayDecision
from trading_live_claude.logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_JOURNAL = "state/intel_overlay.jsonl"

# Scalar snapshot fields worth tracking through time. Dict-valued fields (per-country counts,
# per-domain acceleration) are flattened separately below.
SCALAR_FIELDS: tuple[str, ...] = (
    "global_alert_count", "global_max_importance", "conflict_events_active",
    "natural_disasters_active", "energy_stress", "strategic_risk", "fear_greed",
)

# Below this many complete records, derived features return neutral rather than fake precision.
MIN_HISTORY = 8


def load_journal(path: str | Path = DEFAULT_JOURNAL, *, complete_only: bool = True) -> pd.DataFrame:
    """Load the overlay journal into a time-indexed frame, one row per recorded read.

    Degraded records (partial fetches) are dropped by default: their missing inputs would read as
    genuine drops in risk and corrupt every delta computed across them.
    """
    p = Path(path)
    if not p.exists():
        return pd.DataFrame()
    rows: list[dict[str, object]] = []
    for line in p.read_text(encoding="utf-8").strip().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        snap = rec.get("snapshot", {})
        if complete_only and snap.get("degraded"):
            continue
        row: dict[str, object] = {"as_of": pd.to_datetime(rec.get("as_of"), utc=True)}
        for f in SCALAR_FIELDS:
            v = snap.get(f)
            row[f] = float(v) if isinstance(v, (int, float)) else np.nan
        for dom, val in (snap.get("event_acceleration") or {}).items():
            row[f"accel_{dom}"] = float(val)
        row["vix"] = float((snap.get("market") or {}).get("equity_vol", np.nan))
        row["n_countries"] = float(len(snap.get("country_alert_counts") or {}))
        # decisions are journaled alongside the snapshot; keep the per-class scalars
        for cls, dec in (rec.get("decisions") or {}).items():
            if isinstance(dec, dict) and "scalar" in dec:
                row[f"scalar_{cls}"] = float(dec["scalar"])
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("as_of").set_index("as_of")
    return df[~df.index.duplicated(keep="last")]


@dataclass(frozen=True)
class IntelFeature:
    """One derived view of a tracked field."""

    name: str
    latest: float
    delta: float          # change vs the previous read (NaN when unavailable)
    pct_rank: float       # position within its own recorded history, [0, 1] (0.5 = neutral)
    run_length: int       # consecutive reads above its own quiet baseline (persistence)
    trend: str            # "rising" | "falling" | "flat"


def _run_length(series: pd.Series) -> int:
    """Consecutive reads (ending at the latest) that sit ABOVE this field's own quiet baseline.

    The baseline is the 25th percentile, not the median. A median threshold fails at both ends: on a
    mostly-flat series the median *is* the baseline, so every read counts as elevated; on a mostly-
    elevated series the median *is* the elevated level, so none do. The quiet quartile is stable
    under both — it answers "how long has this been above what this field looks like when calm".
    """
    baseline = float(series.quantile(0.25))
    n = 0
    for v in reversed(series.tolist()):
        if pd.isna(v) or v <= baseline:
            break
        n += 1
    return n


def derive(df: pd.DataFrame, *, min_history: int = MIN_HISTORY) -> dict[str, IntelFeature]:
    """Derived features per tracked column. Thin history -> neutral values, never invented ones."""
    out: dict[str, IntelFeature] = {}
    if df.empty:
        return out
    enough = len(df) >= min_history
    for col in df.columns:
        s = df[col].dropna()
        if s.empty:
            continue
        latest = float(s.iloc[-1])
        delta = float(s.iloc[-1] - s.iloc[-2]) if len(s) >= 2 else float("nan")
        if enough:
            pct = float((s < latest).mean() + 0.5 * (s == latest).mean())
            run = _run_length(s)
            trend = "rising" if delta > 0 else "falling" if delta < 0 else "flat"
        else:
            # Not enough recorded history to say where this sits or how long it has held.
            pct, run, trend = 0.5, 0, "flat"
        out[col] = IntelFeature(name=col, latest=latest, delta=delta, pct_rank=pct,
                                run_length=run, trend=trend)
    return out


class IntelHistory:
    """Journal-backed feature view, for enriching the live overlay and interpretation."""

    def __init__(self, path: str | Path = DEFAULT_JOURNAL, *, min_history: int = MIN_HISTORY) -> None:
        self.path = Path(path)
        self.min_history = min_history
        self.frame = load_journal(self.path)
        self.features = derive(self.frame, min_history=min_history)

    @property
    def depth(self) -> int:
        return len(self.frame)

    @property
    def is_usable(self) -> bool:
        """Whether the journal holds enough reads for the relative/persistence features to mean anything."""
        return self.depth >= self.min_history

    def span_hours(self) -> float:
        if self.depth < 2:
            return 0.0
        return float((self.frame.index[-1] - self.frame.index[0]).total_seconds() / 3600.0)

    def get(self, name: str) -> IntelFeature | None:
        return self.features.get(name)

    def summary(self) -> str:
        """One-line-per-feature digest, for the CLI and for eyeballing what the feed has learned."""
        if not self.features:
            return "intel history: empty journal"
        head = (f"intel history: {self.depth} reads over {self.span_hours():.1f}h"
                f"{'' if self.is_usable else f' (need {self.min_history} for relative features)'}")
        lines = [head]
        for f in self.features.values():
            d = "" if np.isnan(f.delta) else f"  d{f.delta:+.2f}"
            lines.append(f"  {f.name:22} {f.latest:>8.2f}{d:>10}  pct {f.pct_rank:.2f}  "
                         f"run {f.run_length}  {f.trend}")
        return "\n".join(lines)


def append_snapshot(snapshot: IntelSnapshot,
                    decisions: Mapping[Any, OverlayDecision] | None = None,
                    path: str | Path = DEFAULT_JOURNAL) -> None:
    """Append one observed snapshot to the journal. Call this from EVERY path that obtains one.

    The journal is the only point-in-time record this project will ever have of the OSINT feed — the
    vendor keeps no queryable history — so every read is worth keeping, including reads taken by the
    live monitor and by analysis scripts, not just by the CLI command. Accumulation is the whole
    asset: the relative and persistence features in this module only become meaningful with depth,
    and eventually this is what makes an OSINT feature honestly backtestable.

    Degraded snapshots ARE written (with their flag) so gaps in the feed are themselves on the
    record; :func:`load_journal` filters them out when deriving features. Failures here never
    propagate — losing one journal line must not break a trading loop.
    """
    try:
        snap = asdict(snapshot)
        rec: dict[str, object] = {
            "as_of": pd.Timestamp.now(tz="UTC").isoformat(),
            "snapshot": {k: v for k, v in snap.items() if k != "as_of"},
        }
        if decisions:
            rec["decisions"] = {
                c: {"scalar": d.scalar, "halt": d.halt_new_entries, "reasons": list(d.reasons)}
                for c, d in decisions.items()
            }
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + chr(10))
    except Exception:  # never let journaling break a caller
        log.warning("intel.journal.append_failed", path=str(path))
