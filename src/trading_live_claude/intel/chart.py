"""Render the per-asset-class overlay decision as a risk-gauge PNG."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from trading_live_claude.intel.overlay import OVERLAY_CLASSES, OverlayClass, OverlayDecision


def _color(scalar: float) -> str:
    if scalar >= 0.7:
        return "#2E9B8F"   # calm — teal
    if scalar > 0.4:
        return "#E0A44A"   # caution — amber (still ok, just trimmed)
    return "#C0432C"       # stood down (halted) — vermilion


def render_overlay_chart(decisions: Mapping[OverlayClass, OverlayDecision], out_path: str | Path,
                         *, as_of: datetime | None = None, degraded: bool = False) -> Path:
    """Horizontal risk gauge: one bar per asset class, filled to its overlay scalar."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    classes = [c for c in OVERLAY_CLASSES if c in decisions]
    scalars = [decisions[c].scalar for c in classes]
    colors = [_color(v) for v in scalars]

    fig, ax = plt.subplots(figsize=(9, 4.6))
    y = range(len(classes))
    ax.barh(list(y), [1.0] * len(classes), color="#E9ECF1", height=0.62, zorder=1)
    ax.barh(list(y), scalars, color=colors, height=0.62, zorder=2)

    for i, c in enumerate(classes):
        d = decisions[c]
        ax.text(0.012, i, c.upper(), va="center", ha="left", fontsize=10, fontweight="bold",
                color="#182130", zorder=3)
        label = f"{d.scalar:.0%}" + ("  HALT" if d.halt_new_entries else "")
        ax.text(min(d.scalar + 0.015, 0.99), i, label, va="center",
                ha="left" if d.scalar < 0.85 else "right", fontsize=10, color="#182130", zorder=3)
        reason = d.reasons[0] if d.reasons else "no elevated risk"
        ax.text(0.99, i - 0.3, reason, va="center", ha="right", fontsize=7.5,
                color="#6C7A93", style="italic", zorder=3)

    ax.set_xlim(0, 1)
    ax.set_ylim(-0.6, len(classes) - 0.4)
    ax.invert_yaxis()
    ax.set_yticks([])
    ax.set_xticks([0, 0.25, 0.5, 0.75, 1.0])
    ax.set_xticklabels(["0", "25%", "50%", "75%", "full"])
    ax.axvline(0.4, color="#C0432C", ls=":", lw=1, alpha=0.7, zorder=4)
    stamp = (as_of or datetime.now()).strftime("%Y-%m-%d %H:%M UTC")
    title = "WorldMonitor risk overlay — gross-exposure scalar by asset class"
    if degraded:
        title += "  (feed degraded — capped)"
    ax.set_title(title, fontsize=12, fontweight="bold", color="#182130", loc="left")
    ax.text(1.0, 1.06, stamp, transform=ax.transAxes, ha="right", fontsize=8, color="#6C7A93")
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return out
