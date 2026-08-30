"""Build a 50-asset, 5-class basket and process it through the live WorldMonitor OSINT overlay.

Ten assets in each overlay class (equity / future / commodity / fx / crypto). We start from an
equal-weight book (2% each) and apply the live per-asset-class risk scalar — de-risk only — to get a
risk-adjusted book: trimmed exposure per class, new entries halted where the overlay says stand down,
freed weight to cash. World-time OSINT: the snapshot is fetched live at run time.

Run:  python -m uv run python scripts/osint_basket.py
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from trading_live_claude.config.settings import Settings
from trading_live_claude.intel.overlay import IntelSnapshot, OverlayClass, RiskOverlay
from trading_live_claude.intel.worldmonitor import WorldMonitorClient

# 50 assets, ten per overlay class. Symbols chosen so the flow spans real, liquid instruments.
BASKET: dict[OverlayClass, list[str]] = {
    "equity": ["RY.TO", "BNS.TO", "ENB.TO", "CNQ.TO", "SHOP.TO",
               "AAPL", "MSFT", "JPM", "XOM", "SPY"],
    "future": ["/ES", "/NQ", "/YM", "/CL", "/GC", "/SI", "/ZB", "/ZN", "/ZC", "/6E"],
    "commodity": ["GLD", "IAU", "SLV", "PSLV", "USO", "UNG", "DBC", "GSG", "CGL.TO", "CPER"],
    "fx": ["USDCAD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
           "USDCHF", "NZDUSD", "USDMXN", "EURJPY", "EURGBP"],
    "crypto": ["BTC/USD", "ETH/USD", "XMR/USD", "XRP/USD", "XLM/USD",
               "LINK/USD", "SOL/USD", "ADA/USD", "DOGE/USD", "PAXG/USD"],
}
CLASS_ORDER: list[OverlayClass] = ["equity", "future", "commodity", "fx", "crypto"]
CLASS_COLOR = {"equity": "#3B6EA5", "future": "#7A5AA6", "commodity": "#B4791F",
               "fx": "#1E7E74", "crypto": "#C0562C"}


async def _live_snapshot() -> IntelSnapshot:
    key = Settings().worldmonitor_api_key
    if not key:
        raise SystemExit("WORLDMONITOR_API_KEY not set — cannot fetch live OSINT snapshot.")
    async with WorldMonitorClient(key) as wm:
        return await wm.snapshot()


def journal_snapshot(path: str = "state/intel_overlay.jsonl") -> tuple[IntelSnapshot, str]:
    """Replay the most recent COMPLETE journaled snapshot — no network.

    The overlay journals every live read, so a rate-limited or degraded moment does not cost us the
    analysis: we re-score the last good snapshot offline. Degraded records (partial fetches) are
    skipped, since scoring them would understate risk with missing inputs.
    """
    import json
    from pathlib import Path

    lines = Path(path).read_text(encoding="utf-8").strip().splitlines()
    good = []
    for line in lines:
        rec = json.loads(line)
        snap = rec.get("snapshot", {})
        if not snap.get("degraded") and snap.get("fear_greed") is not None:
            good.append((rec.get("as_of", "?"), snap))
    if not good:
        raise SystemExit(f"no complete (non-degraded) snapshot in {path}")
    as_of, s = good[-1]
    fields = {f for f in IntelSnapshot.__dataclass_fields__ if f != "as_of"}
    return IntelSnapshot(**{k: v for k, v in s.items() if k in fields}), as_of


def get_snapshot(from_journal: bool = False) -> tuple[IntelSnapshot, str]:
    """Live snapshot, falling back to the journal when the API is unavailable/rate-limited."""
    if not from_journal:
        try:
            snap = asyncio.run(_live_snapshot())
            if not snap.degraded:
                return snap, "live"
            print("  (live read degraded — falling back to the last complete journaled snapshot)")
        except Exception as e:
            print(f"  (live read failed: {type(e).__name__} — falling back to journal)")
    return journal_snapshot()


def render(basket, decisions, snap, out="reports/osint_basket.png"):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n = sum(len(v) for v in basket.values())
    base = 1.0 / n  # equal-weight starting book
    rows = []
    for cls in CLASS_ORDER:
        d = decisions[cls]
        for sym in basket[cls]:
            rows.append((cls, sym, base, base * d.scalar, d.halt_new_entries))

    fig, ax = plt.subplots(figsize=(11, 12))
    y = list(range(len(rows)))[::-1]
    for yi, (cls, sym, b, adj, halt) in zip(y, rows, strict=True):
        ax.barh(yi, b, color="#E4E7EC", height=0.72, zorder=1)                 # base ghost
        ax.barh(yi, adj, color=CLASS_COLOR[cls], height=0.72, zorder=2,
                hatch="///" if halt else None, edgecolor="white", linewidth=0.4)
        ax.text(-0.0006, yi, sym, va="center", ha="right", fontsize=7.5, color="#182130")
        tag = f"{adj / base:.0%}" + ("  HALT" if halt else "")
        ax.text(adj + 0.00015, yi, tag, va="center", ha="left", fontsize=7, color="#41506A")

    gross = sum(r[3] for r in rows)
    ax.set_xlim(0, base * 1.25)
    ax.set_yticks([])
    ax.set_xlabel(f"per-asset exposure  (equal-weight base {base:.1%}; grey = base, colour = OSINT-adjusted)")
    fg = f"{snap.fear_greed:.0f}" if snap.fear_greed is not None else "n/a"
    ax.set_title("50-asset basket through the live WorldMonitor OSINT overlay\n"
                 f"gross {gross:.0%} of {1.0:.0%}  ·  cash {1 - gross:.0%}  ·  "
                 f"strategic-risk {snap.strategic_risk:.0f}/100  ·  fear/greed {fg}  ·  VIX "
                 f"{snap.market.get('equity_vol', float('nan')):.1f}",
                 fontsize=11, fontweight="bold", loc="left", color="#182130")
    handles = [plt.Rectangle((0, 0), 1, 1, color=CLASS_COLOR[c]) for c in CLASS_ORDER]
    labels = [f"{c}  x{decisions[c].scalar:.2f}" + ("  HALT" if decisions[c].halt_new_entries else "")
              for c in CLASS_ORDER]
    ax.legend(handles, labels, loc="lower right", fontsize=9, frameon=False, title="class scalar")
    for sp in ("top", "right", "left"):
        ax.spines[sp].set_visible(False)
    fig.tight_layout()
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(p, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return p, gross


def main() -> None:
    import sys as _s
    snap, src = get_snapshot("--from-journal" in _s.argv)
    print(f"  [snapshot source: {src}]")
    decisions = RiskOverlay().evaluate(snap)
    n = sum(len(v) for v in BASKET.values())
    base = 1.0 / n

    print(f"\n50-asset basket via live OSINT overlay  (strategic-risk {snap.strategic_risk:.0f}, "
          f"fear/greed {snap.fear_greed}, VIX {snap.market.get('equity_vol')}, "
          f"degraded={snap.degraded})\n")
    print(f"{'class':<10} {'scalar':>7} {'entries':>8}  {'adj exposure/asset':>18}  top driver")
    gross = 0.0
    for cls in CLASS_ORDER:
        d = decisions[cls]
        adj = base * d.scalar
        gross += adj * len(BASKET[cls])
        driver = d.reasons[0] if d.reasons else "no elevated risk"
        print(f"{cls:<10} {d.scalar:>6.0%} {'HALT' if d.halt_new_entries else 'ok':>8}  "
              f"{adj:>17.2%}  {driver}")
    print(f"\ngross exposure {gross:.0%}  ·  cash {1 - gross:.0%}  ·  {n} assets")

    p, _ = render(BASKET, decisions, snap)
    print(f"chart -> {p}")


if __name__ == "__main__":
    main()
