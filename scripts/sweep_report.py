"""Render the $1-45 sweep result and intersect the survivors with the OSINT interpretation.

Reads the CSVs the sweep already wrote (no recomputation) plus the last complete journaled OSINT
snapshot (no API calls), and produces one chart with two panels:

* **left** — in-sample score vs out-of-sample score for the walk-forward cohort. The story of every
  sweep in this project is the collapse between them, so it is drawn explicitly.
* **right** — the survivors, tiered, tagged where they sit inside a live intel thesis.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from osint_basket import journal_snapshot

from trading_live_claude.intel.interpret import interpret

INK = "#182130"
MUTED = "#6C7A93"


def theme_of(sym: str, implicated: dict[str, list[str]]) -> str | None:
    """Which intel theme (if any) a swept symbol belongs to. Cache symbols use _ for . separators."""
    norm = sym.replace("_TO", ".TO").replace("_UN_", ".UN.").replace("_", ".")
    for theme, tickers in implicated.items():
        for t in tickers:
            if norm.upper() == t.upper().split()[0]:
                return theme
    return None


def main() -> None:
    wf = pd.read_csv("reports/sweep_1_45_walkforward.csv")
    panel = pd.read_csv("reports/sweep_1_45_panel.csv")
    snap, as_of = journal_snapshot()
    theses = interpret(snap)

    # An energy/materials producer is implicated by the energy thesis even when it is not one of the
    # module's exemplar tickers, so tag by explicit membership rather than pretending to a sector DB.
    ENERGY_SWEPT = {"ARX_TO", "KEL_TO", "XEG_TO", "PSK_TO"}
    MATERIALS_SWEPT = {"VALE"}

    fig, (axL, axR) = plt.subplots(1, 2, figsize=(14.5, 6.4), width_ratios=[1.15, 1])

    # ---- left: in-sample vs out-of-sample -------------------------------------------------
    d = wf.sort_values("is_score", ascending=False)
    y = range(len(d))
    axL.barh(list(y), d["is_score"], color="#CBD2DA", height=0.66, label="in-sample score", zorder=2)
    colors = ["#1E7E74" if t == "robust" else "#C0562C" for t in d["tier"]]
    axL.barh(list(y), d["oos_score"], color=colors, height=0.36, label="out-of-sample (real)", zorder=3)
    axL.set_yticks(list(y))
    axL.set_yticklabels([s.replace("_TO", ".TO").replace("_UN_", ".UN.") for s in d["sym"]], fontsize=9)
    axL.invert_yaxis()
    axL.set_xlabel("sortino_over_dd score")
    axL.legend(loc="lower right", frameon=False, fontsize=9)
    axL.set_title("In-sample promise vs out-of-sample reality\n"
                  "the gap IS the finding — top in-sample names collapse",
                  fontsize=11, fontweight="bold", loc="left", color=INK)
    for i, (_, r) in enumerate(d.iterrows()):
        axL.text(max(r["is_score"], r["oos_score"]) + 0.35, i, f"WFE {r['wfe']:.2f}",
                 va="center", fontsize=7.5, color=MUTED)
    for sp in ("top", "right"):
        axL.spines[sp].set_visible(False)

    # ---- right: survivors + intel tagging -------------------------------------------------
    rob = wf[wf.tier == "robust"].sort_values("oos_score", ascending=False)
    axR.axis("off")
    axR.set_title("Walk-forward survivors, crossed with live OSINT",
                  fontsize=11, fontweight="bold", loc="left", color=INK)
    ytop = 0.93
    axR.text(0.0, ytop, f"{len(panel)} screened  ->  {len(wf)} walk-forwarded  ->  "
             f"{len(rob)} robust", fontsize=10, color=INK, fontweight="bold",
             transform=axR.transAxes)
    ytop -= 0.08
    for _, r in rob.iterrows():
        sym = r["sym"].replace("_TO", ".TO").replace("_UN_", ".UN.")
        tag = ("energy" if r["sym"] in ENERGY_SWEPT
               else "materials" if r["sym"] in MATERIALS_SWEPT else None)
        axR.text(0.0, ytop, f"{sym}", fontsize=11, fontweight="bold", color="#1E7E74",
                 transform=axR.transAxes)
        axR.text(0.20, ytop, f"${r['price']:.2f}  {r['strategy']}", fontsize=9, color=INK,
                 transform=axR.transAxes)
        axR.text(0.0, ytop - 0.045,
                 f"   OOS {r['oos_score']:.2f}   WFE {r['wfe']:.2f}   "
                 f"{int(r['oos_trades'])} trades   maxDD {r['oos_maxdd']:.1%}",
                 fontsize=8.5, color=MUTED, transform=axR.transAxes)
        if tag:
            axR.text(0.0, ytop - 0.085, f"   ** inside the live '{tag}' intel thesis",
                     fontsize=8.5, color="#B4791F", style="italic", transform=axR.transAxes)
            ytop -= 0.04
        ytop -= 0.135

    ytop -= 0.02
    axR.text(0.0, ytop, "Live OSINT theses (hypotheses, not signals)", fontsize=9.5,
             fontweight="bold", color=INK, transform=axR.transAxes)
    ytop -= 0.05
    for t in theses[:3]:
        axR.text(0.0, ytop, f"• {t.name} [{t.confidence}]", fontsize=8.5, color=INK,
                 transform=axR.transAxes)
        ytop -= 0.045
    axR.text(0.0, ytop - 0.02, f"snapshot {as_of[:16]}  ·  offline replay, no API calls",
             fontsize=7.5, color=MUTED, style="italic", transform=axR.transAxes)

    fig.tight_layout()
    out = Path("reports/sweep_1_45.png")
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"chart -> {out}")


if __name__ == "__main__":
    main()
