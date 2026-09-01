"""Per-session + combined report for the dual-format (QT + Kraken) paper trading state.

Reads the three paper journals — ``state/paper_orders.jsonl``, ``state/paper_fills.jsonl``,
``state/paper_equity.csv`` — groups by ``session_id`` (the field item 5 added), and produces:

* ``reports/paper_report.md`` — one summary table per session plus an aggregate line, and a fills
  table per session. Written even when there is only one session so the deliverable is stable.
* ``reports/paper_report.png`` — small-multiple equity curves (one panel per session) and a
  cash-vs-positions bar for the most recent state of each session.

Venue is inferred from the symbol shape: a ``/`` implies a Kraken pair, everything else is treated
as Questrade equity. That is honest enough for the current two-venue layout and needs no explicit
tagging in the journal.

Legacy fills. The pre-item-5 rows (no ``session_id`` field) are still read but bucketed as
``legacy``; their equity was never journaled and so they contribute to the fills table only, not
to the equity chart. Kept visible rather than hidden so nothing about the earlier stacking bug is
silently swept out of the record.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

STATE = Path("state")
REPORTS = Path("reports")
FILLS = STATE / "paper_fills.jsonl"
ORDERS = STATE / "paper_orders.jsonl"
EQUITY = STATE / "paper_equity.csv"
OUT_MD = REPORTS / "paper_report.md"
OUT_PNG = REPORTS / "paper_report.png"


# ---- loaders -----------------------------------------------------------------


def _load_jsonl(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return pd.DataFrame(rows)


def _load_equity() -> pd.DataFrame:
    if not EQUITY.exists():
        return pd.DataFrame(columns=["ts", "session_id", "equity", "cash", "positions_value",
                                     "realized_pnl", "unrealized_pnl", "peak_equity",
                                     "drawdown_pct"])
    df = pd.read_csv(EQUITY)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.sort_values("ts").reset_index(drop=True)


# ---- inference ---------------------------------------------------------------


def venue_of(symbol: str) -> str:
    """Kraken pairs contain a slash; anything else is treated as a Questrade equity."""
    return "kraken" if "/" in symbol else "questrade"


def _short(sid: str) -> str:
    return "legacy" if not sid or pd.isna(sid) else sid[:8]


# ---- report bodies -----------------------------------------------------------


def build_session_summary(fills: pd.DataFrame, equity: pd.DataFrame) -> pd.DataFrame:
    """One row per session_id. Combines the fills journal and the latest equity snapshot."""
    if fills.empty and equity.empty:
        return pd.DataFrame()

    # session_id may be missing on legacy fills; label them so they still surface.
    if "session_id" not in fills.columns:
        fills = fills.copy()
        fills["session_id"] = None
    fills = fills.copy()
    fills["session_id"] = fills["session_id"].fillna("legacy")
    fills["venue"] = fills["symbol"].map(venue_of)

    rows: list[dict[str, object]] = []
    for sid, grp in fills.groupby("session_id"):
        symbols = sorted(grp["symbol"].unique().tolist())
        venues = sorted(grp["venue"].unique().tolist())
        eq_rows = equity[equity["session_id"] == sid] if sid != "legacy" else pd.DataFrame()
        latest_equity = float(eq_rows["equity"].iloc[-1]) if not eq_rows.empty else float("nan")
        peak = float(eq_rows["peak_equity"].iloc[-1]) if not eq_rows.empty else float("nan")
        dd = float(eq_rows["drawdown_pct"].iloc[-1]) if not eq_rows.empty else float("nan")
        realized = float(eq_rows["realized_pnl"].iloc[-1]) if not eq_rows.empty else 0.0
        unrealized = float(eq_rows["unrealized_pnl"].iloc[-1]) if not eq_rows.empty else float("nan")
        rows.append({
            "session_id": _short(sid),
            "venue(s)": ",".join(venues),
            "fills": int(len(grp)),
            "symbols": ",".join(symbols),
            "notional": float((grp["quantity"] * grp["price"]).sum()),
            "commissions": float(grp["commission"].sum()) if "commission" in grp.columns else 0.0,
            "equity": latest_equity,
            "peak_equity": peak,
            "drawdown_%": dd * 100.0 if not pd.isna(dd) else float("nan"),
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
        })
    df = pd.DataFrame(rows).sort_values(["venue(s)", "session_id"])
    return df


def render_markdown(summary: pd.DataFrame, fills: pd.DataFrame,
                    orders: pd.DataFrame, equity: pd.DataFrame) -> str:
    parts: list[str] = ["# Dual-format paper-trading report",
                        "",
                        f"Generated {pd.Timestamp.now(tz='UTC').isoformat()}. "
                        f"Journals under `{STATE}/`.",
                        ""]

    if summary.empty:
        parts.append("_No fills journaled yet._")
        return "\n".join(parts)

    # ---- headline
    total_notional = summary["notional"].sum()
    total_fills = int(summary["fills"].sum())
    active = summary[summary["session_id"] != "legacy"]
    parts.extend([
        f"**Sessions:** {len(summary)} total ({len(active)} with equity tracking, "
        f"{len(summary) - len(active)} legacy pre-item-5).",
        f"**Fills:** {total_fills}. **Combined notional:** ${total_notional:,.0f}.",
        "",
    ])

    # ---- per-session table
    parts.extend(["## Per-session summary", ""])
    parts.append(summary.to_markdown(index=False, floatfmt=",.2f"))
    parts.append("")

    # ---- fills table per session
    parts.extend(["## Fills detail", ""])
    fills = fills.copy()
    if "session_id" not in fills.columns:
        fills["session_id"] = "legacy"
    fills["session_id"] = fills["session_id"].fillna("legacy")
    fills["venue"] = fills["symbol"].map(venue_of)
    fills["fill_time"] = pd.to_datetime(fills["fill_time"], utc=True)
    for sid, grp in fills.groupby("session_id"):
        parts.append(f"### session `{_short(sid)}`  ({','.join(sorted(grp['venue'].unique()))})")
        parts.append("")
        cols = ["fill_time", "symbol", "side", "quantity", "price", "commission"]
        parts.append(grp.sort_values("fill_time")[cols].to_markdown(index=False, floatfmt=",.4f"))
        parts.append("")

    # ---- rejected intents, so the funnel is visible
    if not orders.empty and "accepted" in orders.columns:
        rej = orders[orders["accepted"] == False]           # noqa: E712 - explicit for pandas
        if not rej.empty:
            parts.extend(["## Rejected intents (paper_orders.jsonl)", ""])
            rej_display = rej.copy()
            rej_display["session_id"] = rej_display["session_id"].map(_short)
            keep = [c for c in ("session_id", "ts", "symbol", "action", "shares",
                                 "ref_price", "rejected_reasons") if c in rej_display.columns]
            parts.append(rej_display[keep].to_markdown(index=False))
            parts.append("")

    # ---- safety recap
    parts.extend([
        "## Safety recap",
        "",
        "- All fills carry `\"venue\": \"paper\"` and route through `PaperBroker`; "
        "the real Questrade and Kraken accounts are untouched.",
        "- `KrakenBroker.place_order` refuses unless `enable_live_orders=True` at construction — "
        "nothing in the paper flow flips it.",
        "- `session_id` on every row keeps the two runs separable in the record so P&L per "
        "session is honest.",
        "",
    ])
    return "\n".join(parts)


# ---- charts ------------------------------------------------------------------


def render_chart(summary: pd.DataFrame, equity: pd.DataFrame) -> None:
    """Small-multiple equity curves + a cash/positions breakdown bar per active session."""
    active = summary[summary["session_id"] != "legacy"]
    if active.empty or equity.empty:
        return
    n = len(active)
    fig = plt.figure(figsize=(11.0, 3.2 + 2.6 * ((n + 1) // 2)))
    gs = fig.add_gridspec(nrows=max(2, (n + 1) // 2), ncols=2, hspace=0.55, wspace=0.35,
                           height_ratios=[1.0] * max(2, (n + 1) // 2))

    for i, (_, s) in enumerate(active.iterrows()):
        ax = fig.add_subplot(gs[i // 2, i % 2])
        # match by short session id
        rows = equity[equity["session_id"].str.startswith(s["session_id"])]
        if rows.empty:
            continue
        ax.plot(rows["ts"], rows["equity"], marker="o", linewidth=1.4)
        ax.set_title(f"{s['venue(s)']}  ·  session {s['session_id']}", fontsize=10)
        ax.set_ylabel("equity ($)")
        ax.axhline(rows["peak_equity"].iloc[-1], color="tab:gray", linestyle=":", linewidth=0.8,
                   label=f"peak {rows['peak_equity'].iloc[-1]:,.0f}")
        ax.legend(loc="lower left", fontsize=8)
        for lbl in ax.get_xticklabels():
            lbl.set_rotation(20)
            lbl.set_ha("right")

    # final panel — a stacked bar of cash vs positions per active session
    ax_bar = fig.add_subplot(gs[-1, :]) if n <= 2 else None
    if ax_bar is not None:
        latest = (equity.sort_values("ts").groupby("session_id").tail(1)
                       .set_index("session_id"))
        # Only keep sessions that appear in the active summary.
        keep = [sid for sid in latest.index
                if any(sid.startswith(s) for s in active["session_id"].tolist())]
        latest = latest.loc[keep]
        x = [sid[:8] for sid in latest.index]
        ax_bar.bar(x, latest["cash"], label="cash", color="tab:blue")
        ax_bar.bar(x, latest["positions_value"], bottom=latest["cash"],
                    label="positions", color="tab:orange")
        ax_bar.set_ylabel("$")
        ax_bar.set_title("cash vs positions, latest snapshot per session", fontsize=10)
        ax_bar.legend(loc="upper right", fontsize=8)

    fig.suptitle("Paper trading — QT + Kraken", fontsize=12, y=0.995)
    REPORTS.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PNG, dpi=140, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    fills = _load_jsonl(FILLS)
    orders = _load_jsonl(ORDERS)
    equity = _load_equity()
    summary = build_session_summary(fills, equity)

    REPORTS.mkdir(parents=True, exist_ok=True)
    md = render_markdown(summary, fills, orders, equity)
    OUT_MD.write_text(md, encoding="utf-8")
    render_chart(summary, equity)
    print(f"[paper_report] wrote {OUT_MD} and {OUT_PNG}")
    if not summary.empty:
        print("[paper_report] session summary:")
        print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
