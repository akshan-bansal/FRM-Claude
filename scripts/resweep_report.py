"""Render the ``resweep_full`` output as a human-readable end-to-end summary.

Reads ``reports/sweep_resweep_full_walkforward.csv`` (produced by ``scripts/sweep_universe.py
--tag resweep_full``) and writes:

* ``reports/resweep_full.md`` — top-line stats, robust survivors, held-in-pool ratings, and a
  compact table of every WF-tested name sorted by out-of-sample score.
* ``reports/resweep_full.png`` — three panels: OOS-vs-IS scatter (the "in-sample overselling"
  story), OOS score bar chart of survivors, and a WFE distribution histogram.

Cheap to re-run; reads two CSVs and writes two files. Intended follow-up after the sweep
completes so the reader is not scrolling through a raw CSV to see what happened.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

WF_PATH = Path("reports/sweep_resweep_full_walkforward.csv")
PANEL_PATH = Path("reports/sweep_resweep_full_panel.csv")
OUT_MD = Path("reports/resweep_full.md")
OUT_PNG = Path("reports/resweep_full.png")


def _load() -> tuple[pd.DataFrame, pd.DataFrame]:
    wf = pd.read_csv(WF_PATH) if WF_PATH.exists() else pd.DataFrame()
    panel = pd.read_csv(PANEL_PATH) if PANEL_PATH.exists() else pd.DataFrame()
    return wf, panel


def _md(wf: pd.DataFrame, panel: pd.DataFrame) -> str:
    if wf.empty:
        return ("# Resweep results\n\n_Walk-forward CSV missing at "
                f"`{WF_PATH}`. Run scripts/sweep_universe.py --tag resweep_full first._")
    lines: list[str] = ["# Full resweep — end-to-end results",
                        "",
                        f"Cached universe screened, walked-forward on {len(wf)} candidates. "
                        f"Held names carried in via HELD_ASSETS."]
    robust = wf[wf.tier == "robust"]
    watch = wf[wf.tier == "watch"]
    held = wf[wf.get("held", False)]
    lines.extend([
        "",
        f"**Robust survivors:** {len(robust)}    "
        f"**Watch:** {len(watch)}    "
        f"**Held-in-pool:** {len(held)}",
        "",
    ])

    if not robust.empty:
        lines.extend(["## Robust survivors", ""])
        cols = ["sym", "strategy", "oos_score", "wfe", "oos_return",
                 "oos_max_drawdown" if "oos_max_drawdown" in robust.columns else "oos_maxdd",
                 "oos_trades", "asset_class"]
        cols = [c for c in cols if c in robust.columns]
        lines.append(robust.sort_values("oos_score", ascending=False)[cols]
                     .to_markdown(index=False, floatfmt=",.3f"))
        lines.append("")

    if not held.empty:
        lines.extend(["## Held-in-pool (currently owned in the QT account)",
                       "",
                       "Reads for names we already hold. A held name showing up in `watch` or "
                       "below the top of the `robust` cohort is a research prompt to inspect "
                       "the current holding — not an auto-sell.",
                       ""])
        cols = ["sym", "strategy", "oos_score", "wfe", "oos_trades", "tier", "asset_class"]
        cols = [c for c in cols if c in held.columns]
        lines.append(held.sort_values("oos_score", ascending=False)[cols]
                     .to_markdown(index=False, floatfmt=",.3f"))
        lines.append("")

    lines.extend(["## Every walk-forward-tested name (top 30 by OOS score)", ""])
    cols = ["sym", "strategy", "is_score", "oos_score", "wfe", "oos_trades", "tier"]
    cols = [c for c in cols if c in wf.columns]
    lines.append(wf.sort_values("oos_score", ascending=False).head(30)[cols]
                 .to_markdown(index=False, floatfmt=",.3f"))
    lines.append("")

    if not panel.empty:
        lines.extend([
            "## Stage 2 (in-sample) — top 15 for comparison",
            "",
            "The 'in-sample overselling' story: names with high IS scores may collapse OOS.",
            "",
        ])
        cols = ["sym", "strategy", "score", "sharpe", "maxdd", "trades"]
        cols = [c for c in cols if c in panel.columns]
        lines.append(panel.head(15)[cols].to_markdown(index=False, floatfmt=",.3f"))
        lines.append("")
    return "\n".join(lines)


def _chart(wf: pd.DataFrame, panel: pd.DataFrame) -> None:
    if wf.empty:
        return
    fig = plt.figure(figsize=(12.5, 4.8))
    gs = fig.add_gridspec(1, 3, wspace=0.35)

    # (1) OOS vs IS scatter
    ax1 = fig.add_subplot(gs[0])
    ax1.scatter(wf.get("is_score", []), wf["oos_score"],
                 c=(wf["tier"] == "robust").map({True: "tab:green", False: "tab:orange"}),
                 alpha=0.75, s=32)
    if "is_score" in wf.columns:
        lim = max(wf["is_score"].max(), wf["oos_score"].max()) * 1.05
        ax1.plot([0, lim], [0, lim], "k:", linewidth=0.8, label="IS = OOS")
        ax1.set_xlim(0, lim)
        ax1.set_ylim(min(0, wf["oos_score"].min() * 1.2), lim)
    ax1.set_xlabel("in-sample score")
    ax1.set_ylabel("out-of-sample score")
    ax1.set_title("in-sample vs OOS — the collapse story", fontsize=10)
    ax1.legend(loc="lower right", fontsize=8)

    # (2) OOS score bar chart of robust survivors
    ax2 = fig.add_subplot(gs[1])
    robust = wf[wf.tier == "robust"].sort_values("oos_score", ascending=False).head(20)
    if not robust.empty:
        colors = ["tab:green" if not h else "tab:blue"
                   for h in robust.get("held", [False] * len(robust))]
        ax2.barh(robust["sym"], robust["oos_score"], color=colors)
        ax2.invert_yaxis()
    ax2.set_xlabel("out-of-sample score")
    ax2.set_title("robust survivors (blue = held in QT account)", fontsize=10)

    # (3) WFE distribution
    ax3 = fig.add_subplot(gs[2])
    ax3.hist(wf["wfe"].clip(-1.0, 5.0), bins=25, color="tab:purple", alpha=0.75)
    ax3.axvline(0.5, color="k", linestyle=":", linewidth=1.0, label="WFE >= 0.5 gate")
    ax3.set_xlabel("walk-forward efficiency (OOS / IS)")
    ax3.set_ylabel("names")
    ax3.set_title("WFE distribution", fontsize=10)
    ax3.legend(loc="upper right", fontsize=8)

    fig.suptitle("Full resweep — reformed universe, held-in-pool carry-in", fontsize=12, y=1.02)
    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PNG, dpi=140, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    wf, panel = _load()
    md = _md(wf, panel)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(md, encoding="utf-8")
    _chart(wf, panel)
    print(f"[resweep_report] wrote {OUT_MD} and {OUT_PNG}")


if __name__ == "__main__":
    main()
