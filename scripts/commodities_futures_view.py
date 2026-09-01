"""Depict the commodities & futures side of the book: what we actually hold, and what gates it.

Two things are worth seeing together. First, how thin the validated selection is in these two classes
compared with equities — the sweep repeatedly surfaces commodity ETFs in-sample and they fail the
walk-forward on trade count. Second, that the live overlay is currently standing both classes down,
and that the commodity half of that decision hinges on one input (energy event acceleration) whose
value predates the sparse-baseline guard, so it is shown as a sensitivity rather than a fact.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).parent))
from osint_basket import journal_snapshot

from trading_live_claude.analysis.universe import SEED_UNIVERSE
from trading_live_claude.analysis.universe import WALK_FORWARD_VALIDATED as W
from trading_live_claude.intel.overlay import RiskOverlay
from trading_live_claude.intel.routing import classify_symbol

INK, MUTED = "#182130", "#6C7A93"
GOLD, PURPLE, TEAL, RED = "#B4791F", "#7A5AA6", "#1E7E74", "#C0432C"

# Commodity-linked names the $1-45 sweep scored (ETFs plus energy/materials producers).
COMMO_SWEPT = {"DBC", "DBA", "CPER", "USO", "UNG", "GLD", "SLV", "IAU", "PSLV",
               "CGL_TO", "XEG_TO", "KEL_TO", "ARX_TO", "PSK_TO", "VALE"}


def main() -> None:
    panel = pd.read_csv("reports/sweep_1_45_panel.csv")
    wf = pd.read_csv("reports/sweep_1_45_walkforward.csv")
    snap, as_of = journal_snapshot()
    ov = RiskOverlay()

    base = ov.evaluate(snap)
    neutral = ov.evaluate(replace(snap, event_acceleration={**snap.event_acceleration, "energy": 1.0}))

    validated = {s: v for s, v in W.items() if classify_symbol(s) in ("commodity", "future")}
    swept = panel[panel.sym.isin(COMMO_SWEPT)].sort_values("score", ascending=False)
    wf_syms = set(wf.sym)

    fig = plt.figure(figsize=(14.2, 7.4))
    gs = fig.add_gridspec(2, 2, width_ratios=[1.25, 1], height_ratios=[1.05, 1], hspace=0.42,
                          wspace=0.22)

    # ---- (a) commodity-linked candidates: in-sample score vs whether they survived -------------
    ax = fig.add_subplot(gs[:, 0])
    d = swept.head(12).iloc[::-1]
    colors = []
    for s in d.sym:
        if s in wf_syms:
            row = wf[wf.sym == s].iloc[0]
            colors.append(TEAL if row.tier == "robust" else RED)
        else:
            colors.append("#CBD2DA")
    ax.barh(range(len(d)), d.score, color=colors, height=0.66)
    ax.set_yticks(range(len(d)))
    ax.set_yticklabels([s.replace("_TO", ".TO") for s in d.sym], fontsize=9)
    ax.set_xlabel("in-sample sortino_over_dd score")
    for i, (_, r) in enumerate(d.iterrows()):
        note = f"{int(r.trades)} trades"
        if r.sym in wf_syms:
            row = wf[wf.sym == r.sym].iloc[0]
            note += f"  |  OOS {row.oos_score:.2f}, WFE {row.wfe:.2f} -> {row.tier}"
        else:
            note += "  |  not walk-forwarded"
        ax.text(r.score + 0.15, i, note, va="center", fontsize=7.5, color=MUTED)
    ax.set_xlim(0, float(d.score.max()) * 1.85)
    ax.set_title("Commodity-linked candidates from the $1-45 sweep\n"
                 "green = cleared walk-forward, red = failed it, grey = never reached it",
                 fontsize=11, fontweight="bold", loc="left", color=INK)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)

    # ---- (b) what is actually validated in these classes ---------------------------------------
    ax2 = fig.add_subplot(gs[0, 1])
    ax2.axis("off")
    ax2.set_title("What we actually hold here", fontsize=11, fontweight="bold", loc="left", color=INK)
    y = 0.80
    ax2.text(0.0, y, f"COMMODITY — {len([1 for s in validated if classify_symbol(s) == 'commodity'])} "
             f"validated of {len(SEED_UNIVERSE['commodity'])} seeded",
             fontsize=9.5, fontweight="bold", color=GOLD, transform=ax2.transAxes)
    y -= 0.15
    for s, v in validated.items():
        if classify_symbol(s) != "commodity":
            continue
        ax2.text(0.02, y, f"{s}  {v.strategy}  OOS {v.oos_score:.2f}  WFE {v.wfe:.2f}  "
                 f"{v.oos_trades} trades  [{v.tier}]", fontsize=8.5, color=INK,
                 transform=ax2.transAxes)
        y -= 0.13
    y -= 0.04
    ax2.text(0.0, y, f"FUTURES — 0 validated of {len(SEED_UNIVERSE['future'])} seeded",
             fontsize=9.5, fontweight="bold", color=PURPLE, transform=ax2.transAxes)
    y -= 0.14
    ax2.text(0.02, y, "no futures contract has ever cleared walk-forward here;", fontsize=8.5,
             color=MUTED, transform=ax2.transAxes)
    y -= 0.12
    ax2.text(0.02, y, "the seeds (ES/NQ/YM/RTY/ZN/ZB) are unpriced in the cache.",
             fontsize=8.5, color=MUTED, transform=ax2.transAxes)

    # ---- (c) overlay stance + the sensitivity that decides commodity ---------------------------
    ax3 = fig.add_subplot(gs[1, 1])
    labels = ["commodity", "future"]
    x = range(len(labels))
    b1 = [base[c].scalar for c in labels]
    b2 = [neutral[c].scalar for c in labels]
    ax3.bar([i - 0.19 for i in x], b1, width=0.36, color=[GOLD, PURPLE], label="journaled energy 6.3x")
    ax3.bar([i + 0.19 for i in x], b2, width=0.36, color=[GOLD, PURPLE], alpha=0.45,
            hatch="//", label="if guard neutralises energy")
    ax3.axhline(0.4, color=RED, ls=":", lw=1.2)
    ax3.text(1.46, 0.42, "halt line", fontsize=7.5, color=RED)
    for i, c in enumerate(labels):
        ax3.text(i - 0.19, b1[i] + 0.02, f"{b1[i]:.0%}\n{'HALT' if base[c].halt_new_entries else 'ok'}",
                 ha="center", fontsize=8, color=INK)
        ax3.text(i + 0.19, b2[i] + 0.02,
                 f"{b2[i]:.0%}\n{'HALT' if neutral[c].halt_new_entries else 'ok'}",
                 ha="center", fontsize=8, color=INK)
    ax3.set_xticks(list(x))
    ax3.set_xticklabels([c.upper() for c in labels])
    ax3.set_ylim(0, 0.62)
    ax3.set_ylabel("overlay scalar")
    ax3.legend(fontsize=7.5, frameon=False, loc="upper right")
    ax3.set_title("Live overlay — and what the energy input decides",
                  fontsize=10.5, fontweight="bold", loc="left", color=INK)
    for sp in ("top", "right"):
        ax3.spines[sp].set_visible(False)

    fig.suptitle("Commodities & futures: a thin selection, gated by one unverified input",
                 fontsize=13, fontweight="bold", x=0.008, ha="left", color=INK)
    fig.text(0.008, 0.005, f"OSINT snapshot {as_of[:16]} (offline replay). The 6.3x energy "
             f"acceleration predates the sparse-baseline guard and is pending re-verification.",
             fontsize=7.5, color=MUTED, style="italic")
    out = Path("reports/commodities_futures.png")
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"chart -> {out}")


if __name__ == "__main__":
    main()
