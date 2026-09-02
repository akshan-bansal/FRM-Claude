"""Static HTML dashboard — one file, plotly, regenerated on ``--refresh N`` loop or on-demand.

Reads the journals the paper monitors + graph journal + overlay poller write into ``state/``,
plus the WF-validated pool from ``analysis.universe``, and produces a single self-contained
``reports/dashboard.html`` the reader can open in a browser.

Sections (top to bottom):

  1. **System health** — heartbeat pills for QT paper / Kraken paper / Graph Journal, based on
     the last write time of the journal each process owns. Red if stale beyond a threshold.
  2. **Overlay scalars** — the current de-risk scalar per asset class, taken from the last
     complete row of ``state/intel_overlay.jsonl``. A quick "how de-risked are we right now".
  3. **Fired theses** — the interpret() output on the latest snapshot. Empty null when quiet.
  4. **Persistence runs** — for each watched domain, the current ``elevated_in`` consecutive-poll
     count (regime detection).
  5. **Paper sessions** — per-session summary with equity, drawdown, fills, notional. Same
     query the paper_report script already runs.
  6. **Equity curves** — plotly small-multiples, one line per session.
  7. **Allocator bias** — current per-pair Kraken sleeve conviction bias from the correlation-
     aware allocator.
  8. **WF-validated pool** — sortable table with strategy, OOS score, WFE, win rate, tier.
  9. **Graph journal** — edges by predicate, nodes by type, top sources, best-corroborated
     events — same shape as scripts/graph_profile.md but embedded here.

Uses plotly.js loaded from CDN (single HTML file, ~30KB body + one <script src=cdn>). Prints a
progress line on each render. In ``--refresh N``, renders every N seconds and re-reads state.
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


STATE = Path("state")
REPORTS = Path("reports")
OUT_HTML = REPORTS / "dashboard.html"


# ---- data loaders -------------------------------------------------------------------------------


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _load_paper_equity() -> pd.DataFrame:
    p = STATE / "paper_equity.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.sort_values("ts").reset_index(drop=True)


def _mtime_hours(path: Path) -> float | None:
    if not path.exists():
        return None
    return (time.time() - path.stat().st_mtime) / 3600.0


def _venue_of(symbol: str) -> str:
    return "kraken" if "/" in str(symbol) else "questrade"


# ---- sections -----------------------------------------------------------------------------------


def _section(title: str, body_html: str) -> str:
    return f'<section><h2>{title}</h2>{body_html}</section>'


def _pill(label: str, ok: bool, note: str = "") -> str:
    color = "#22c55e" if ok else "#ef4444"
    return (f'<span class="pill" style="background:{color};color:white;padding:.3em .8em;'
            f'border-radius:1em;margin-right:.7em">{label}</span>'
            f'<span class="pill-note">{note}</span>')


def section_health() -> str:
    """Journal-mtime heartbeat for each background loop."""
    # QT paper writes paper_fills.jsonl + paper_orders.jsonl + paper_equity.csv
    # Kraken paper writes the same journals (session_id disambiguates)
    # Graph journal writes state/intel_graph.jsonl
    #
    # A "healthy" loop has written to its journal within N minutes. Threshold picked per source:
    # paper (5min poll → 15 min stale bar) and graph journal (15 min poll → 30 min stale bar).
    pieces: list[str] = []
    equity_age = _mtime_hours(STATE / "paper_equity.csv")
    graph_age = _mtime_hours(STATE / "intel_graph.jsonl")
    overlay_age = _mtime_hours(STATE / "intel_overlay.jsonl")
    fills_age = _mtime_hours(STATE / "paper_fills.jsonl")

    if equity_age is None:
        pieces.append(_pill("PAPER", False, "no paper_equity.csv"))
    else:
        ok = equity_age * 60 < 30
        pieces.append(_pill("PAPER", ok, f"last write {equity_age * 60:.1f} min ago"))

    if graph_age is None:
        pieces.append(_pill("GRAPH JOURNAL", False, "no intel_graph.jsonl"))
    else:
        ok = graph_age * 60 < 30
        pieces.append(_pill("GRAPH JOURNAL", ok, f"last write {graph_age * 60:.1f} min ago"))

    if overlay_age is not None:
        ok = overlay_age * 60 < 30
        pieces.append(_pill("OVERLAY", ok, f"last write {overlay_age * 60:.1f} min ago"))

    body = "<div>" + "<br><br>".join(pieces) + "</div>"
    if fills_age is not None:
        body += f'<p class="muted">Latest fill journaled {fills_age * 60:.1f} min ago.</p>'
    return _section("System health", body)


def section_overlay_scalars() -> str:
    """Latest per-asset-class scalar from the overlay journal — how de-risked each class is now."""
    rows = _load_jsonl(STATE / "intel_overlay.jsonl")
    if not rows:
        return _section("Overlay scalars", "<p class='muted'>No overlay journal yet.</p>")
    last = rows[-1]
    dec = last.get("decisions") or {}
    fig = go.Figure()
    classes = list(dec.keys())
    scalars = [dec[c].get("scalar", 1.0) for c in classes]
    colors = ["#22c55e" if s > 0.7 else "#eab308" if s > 0.4 else "#ef4444" for s in scalars]
    fig.add_trace(go.Bar(x=classes, y=scalars, marker_color=colors,
                          text=[f"{s:.2f}" for s in scalars], textposition="outside"))
    fig.update_layout(height=280, margin=dict(l=40, r=20, t=30, b=40),
                       yaxis=dict(range=[0, 1.05], title="scalar (1.0 = full)"),
                       showlegend=False, template="plotly_white")
    body = (f'<p class="muted">As of {last.get("as_of", "?")} (degraded: '
            f'{last.get("snapshot", {}).get("degraded", "?")}).</p>'
            + fig.to_html(full_html=False, include_plotlyjs=False))
    return _section("Overlay scalars (live de-risk per asset class)", body)


def section_theses() -> str:
    """Run interpret() on the latest journaled snapshot."""
    from trading_live_claude.intel.interpret import interpret
    from trading_live_claude.intel.overlay import IntelSnapshot

    rows = _load_jsonl(STATE / "intel_overlay.jsonl")
    if not rows:
        return _section("Fired theses", "<p class='muted'>No overlay journal yet.</p>")
    snap_dict = rows[-1].get("snapshot", {})
    # Reconstitute an IntelSnapshot with the scalar fields interpret uses.
    try:
        snap = IntelSnapshot(**{k: v for k, v in snap_dict.items()
                                  if k in IntelSnapshot.__dataclass_fields__})
    except Exception as e:
        return _section("Fired theses", f"<p class='muted'>Snapshot rebuild failed: {e}</p>")
    theses = interpret(snap)
    if not theses:
        return _section("Fired theses", "<p class='muted'>Empty (interpret returned nothing).</p>")
    rows_html = []
    for t in theses:
        if t.name == "No notable configuration":
            rows_html.append(f'<tr><td colspan="4"><em>Quiet tape — {t.inference}</em></td></tr>')
            continue
        rows_html.append(
            f'<tr><td><b>{t.name}</b></td>'
            f'<td>{t.confidence}</td>'
            f'<td>{t.inference}</td>'
            f'<td>{", ".join(t.themes) if t.themes else ""}</td></tr>'
        )
    body = ('<table><thead><tr><th>Thesis</th><th>Confidence</th><th>Inference</th>'
            '<th>Themes</th></tr></thead><tbody>'
            + "".join(rows_html) + '</tbody></table>')
    return _section("Fired theses (interpret on latest snapshot)", body)


def section_persistence() -> str:
    """Consecutive-poll count for each watched domain's elevated_in edge."""
    try:
        from trading_live_claude.intel.graph import edge_persistence, load_edges
    except Exception:
        return _section("Graph persistence", "<p class='muted'>intel.graph unavailable.</p>")
    edges = load_edges()
    if not edges:
        return _section("Graph persistence", "<p class='muted'>Graph journal is empty.</p>")
    watched = ("energy", "conflict", "military", "disaster", "economy")
    rows_html = []
    for dom in watched:
        n = edge_persistence(edges, predicate="elevated_in", object=("domain", dom))
        color = "#ef4444" if n >= 5 else "#eab308" if n >= 3 else "#94a3b8"
        rows_html.append(
            f'<tr><td>{dom}</td>'
            f'<td style="color:{color};font-weight:bold">{n}</td>'
            f'<td>{"regime detected" if n >= 5 else "watch" if n >= 3 else "quiet"}</td></tr>'
        )
    body = ('<table><thead><tr><th>Domain</th><th>Elevated-in run</th><th>State</th></tr></thead>'
            '<tbody>' + "".join(rows_html) + '</tbody></table>')
    return _section("Graph persistence (consecutive polls elevated)", body)


def section_paper_summary() -> str:
    """Per-session paper accounting — one row per session_id."""
    fills = _load_jsonl(STATE / "paper_fills.jsonl")
    equity = _load_paper_equity()
    if not fills:
        return _section("Paper sessions", "<p class='muted'>No fills yet.</p>")
    df = pd.DataFrame(fills)
    df["session_id"] = df.get("session_id", pd.NA).fillna("legacy")
    df["venue"] = df["symbol"].map(_venue_of)
    grp = df.groupby("session_id")
    rows_html = []
    for sid, g in grp:
        eq_rows = equity[equity["session_id"] == sid] if sid != "legacy" else pd.DataFrame()
        latest_eq = float(eq_rows["equity"].iloc[-1]) if not eq_rows.empty else float("nan")
        peak_eq = float(eq_rows["peak_equity"].iloc[-1]) if not eq_rows.empty else float("nan")
        dd = float(eq_rows["drawdown_pct"].iloc[-1]) if not eq_rows.empty else float("nan")
        notional = float((g["quantity"] * g["price"]).sum())
        sid_short = "legacy" if sid == "legacy" else sid[:8]
        eq_str = f"${latest_eq:,.0f}" if not pd.isna(latest_eq) else "—"
        dd_str = f"{dd * 100:.2f}%" if not pd.isna(dd) else "—"
        rows_html.append(
            f'<tr><td>{sid_short}</td>'
            f'<td>{",".join(sorted(g["venue"].unique()))}</td>'
            f'<td>{len(g)}</td>'
            f'<td>{",".join(sorted(g["symbol"].unique()))}</td>'
            f'<td>${notional:,.0f}</td>'
            f'<td>{eq_str}</td>'
            f'<td>{dd_str}</td></tr>'
        )
    body = ('<table><thead><tr><th>Session</th><th>Venue</th><th>Fills</th>'
            '<th>Symbols</th><th>Notional</th><th>Equity</th><th>Drawdown</th>'
            '</tr></thead><tbody>' + "".join(rows_html) + '</tbody></table>')
    return _section("Paper sessions", body)


def section_equity_curves() -> str:
    """Small-multiples equity curve per active session."""
    equity = _load_paper_equity()
    if equity.empty:
        return _section("Equity curves", "<p class='muted'>No equity snapshots yet.</p>")
    sessions = list(equity["session_id"].unique())
    n = len(sessions)
    cols = 2
    rows = (n + cols - 1) // cols
    fig = make_subplots(rows=rows, cols=cols,
                         subplot_titles=[s[:8] for s in sessions], vertical_spacing=0.15)
    for i, sid in enumerate(sessions):
        rr = i // cols + 1
        cc = i % cols + 1
        sub = equity[equity["session_id"] == sid]
        fig.add_trace(go.Scatter(x=sub["ts"], y=sub["equity"], mode="lines+markers",
                                   name=sid[:8], showlegend=False,
                                   line=dict(width=1.5)),
                       row=rr, col=cc)
        if not sub.empty:
            peak = float(sub["peak_equity"].iloc[-1])
            fig.add_hline(y=peak, line_dash="dot", line_color="gray",
                          row=rr, col=cc)
    fig.update_layout(height=260 * rows, margin=dict(l=40, r=20, t=40, b=40),
                       template="plotly_white")
    return _section("Equity curves (per session)",
                     fig.to_html(full_html=False, include_plotlyjs=False))


def section_allocator_bias() -> str:
    """Current Kraken sleeve allocator conviction bias."""
    try:
        from scripts.paper_kraken import _compute_allocator_bias
        from trading_live_claude.analysis.universe import CRYPTO_SLEEVE
        bias = _compute_allocator_bias(CRYPTO_SLEEVE)
    except Exception as e:
        return _section("Allocator bias (Kraken sleeve)",
                         f"<p class='muted'>Compute failed: {e}</p>")
    fig = go.Figure()
    syms = sorted(bias, key=lambda s: -bias[s])
    vals = [bias[s] for s in syms]
    colors = ["#22c55e" if v > 1.05 else "#94a3b8" if v > 0.95 else "#ef4444" for v in vals]
    fig.add_trace(go.Bar(x=syms, y=vals, marker_color=colors,
                          text=[f"x{v:.2f}" for v in vals], textposition="outside"))
    fig.add_hline(y=1.0, line_dash="dot", line_color="gray", annotation_text="equal-weight")
    fig.update_layout(height=300, margin=dict(l=40, r=20, t=30, b=40),
                       yaxis=dict(title="conviction multiplier"),
                       showlegend=False, template="plotly_white")
    return _section("Allocator bias — Kraken crypto sleeve",
                     fig.to_html(full_html=False, include_plotlyjs=False))


def section_validated_pool() -> str:
    """WF-validated pool as a sortable table."""
    from trading_live_claude.analysis.universe import WALK_FORWARD_VALIDATED
    rows_html = []
    for sym, e in sorted(WALK_FORWARD_VALIDATED.items(), key=lambda kv: -kv[1].oos_score):
        wr = getattr(e, "oos_win_rate", None)
        wr_str = f"{wr * 100:.1f}%" if wr is not None else "—"
        tier_color = "#22c55e" if e.tier == "robust" else "#eab308"
        rows_html.append(
            f'<tr><td>{sym}</td>'
            f'<td>{e.asset_class}</td>'
            f'<td>{e.strategy}</td>'
            f'<td>{e.oos_score:.2f}</td>'
            f'<td>{e.wfe:.2f}</td>'
            f'<td>{e.oos_trades}</td>'
            f'<td>{wr_str}</td>'
            f'<td style="color:{tier_color};font-weight:bold">{e.tier}</td></tr>'
        )
    body = ('<table id="pool"><thead><tr><th>Symbol</th><th>Class</th><th>Strategy</th>'
            '<th>OOS</th><th>WFE</th><th>Trades</th><th>Win %</th><th>Tier</th>'
            '</tr></thead><tbody>' + "".join(rows_html) + '</tbody></table>')
    return _section(f"WF-validated pool ({len(WALK_FORWARD_VALIDATED)} names)", body)


def section_graph_profile() -> str:
    """Graph journal shape — edges by predicate, top sources, best-corroborated events."""
    try:
        from trading_live_claude.intel.graph import edges_where, load_edges
    except Exception:
        return _section("Graph journal", "<p class='muted'>intel.graph unavailable.</p>")
    edges = load_edges()
    if not edges:
        return _section("Graph journal", "<p class='muted'>Journal is empty.</p>")

    per_pred: Counter[str] = Counter(e.predicate for e in edges)
    nodes: set[tuple[str, str]] = set()
    for e in edges:
        nodes.add(e.subject)
        nodes.add(e.object)
    by_type: Counter[str] = Counter(n[0] for n in nodes)

    src_counts: Counter[str] = Counter()
    for e in edges_where(edges, predicate="mentioned_by"):
        if e.object[0] == "source":
            src_counts[e.object[1]] += 1

    corr: dict[str, set[str]] = defaultdict(set)
    for e in edges_where(edges, predicate="mentioned_by"):
        if e.subject[0] == "event":
            corr[e.subject[1]].add(e.object[1])
    top_corr = sorted(corr.items(), key=lambda kv: -len(kv[1]))[:5]

    def _bars(counter: Counter[str]) -> str:
        items = counter.most_common(10)
        if not items:
            return "<em>none</em>"
        return "".join(
            f'<div style="display:flex;gap:.5em;align-items:center">'
            f'<span style="width:8em">{k}</span>'
            f'<span style="background:#3b82f6;height:1em;display:inline-block;'
            f'width:{min(v * 4, 200)}px"></span> <span>{v}</span></div>'
            for k, v in items
        )

    corr_html = "".join(
        f'<tr><td><code>{ev[:16]}</code></td><td>{len(sources)}</td>'
        f'<td>{", ".join(sorted(sources))}</td></tr>'
        for ev, sources in top_corr
    ) or '<tr><td colspan="3"><em>no per-event corroboration edges yet</em></td></tr>'

    body = f"""
<div style="display:grid;grid-template-columns:1fr 1fr;gap:2em">
  <div>
    <h3>Edges by predicate</h3>
    {_bars(per_pred)}
    <p class="muted">Total edges: {len(edges)}</p>
  </div>
  <div>
    <h3>Nodes by type</h3>
    {_bars(by_type)}
    <p class="muted">Total nodes: {len(nodes)}</p>
  </div>
</div>
<h3 style="margin-top:1.5em">Top sources by mention count</h3>
{_bars(src_counts)}
<h3 style="margin-top:1.5em">Best-corroborated events (distinct sources per event)</h3>
<table><thead><tr><th>Event id</th><th>Sources</th><th>Names</th></tr></thead>
<tbody>{corr_html}</tbody></table>
"""
    return _section("Graph journal", body)


# ---- render -------------------------------------------------------------------------------------


_TEMPLATE = """<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>trading-live-claude · dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif;
          max-width: 1100px; margin: 1em auto; padding: 0 1em; color: #1f2937 }}
  h1 {{ margin: 0 0 .3em; }}
  h2 {{ border-bottom: 1px solid #e5e7eb; padding-bottom: .3em; margin-top: 1.6em; }}
  h3 {{ margin: 0 0 .5em; font-size: 1em; color: #374151 }}
  section {{ margin-bottom: 2em; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .92em }}
  th, td {{ padding: .4em .6em; text-align: left; border-bottom: 1px solid #f1f5f9 }}
  th {{ background: #f9fafb; font-weight: 600 }}
  .muted {{ color: #6b7280; font-size: .9em }}
  .header {{ display: flex; justify-content: space-between; align-items: baseline;
             border-bottom: 2px solid #1f2937; padding-bottom: .3em; margin-bottom: 1em }}
  code {{ background: #f1f5f9; padding: .1em .3em; border-radius: 3px; font-size: .9em }}
</style>
</head><body>
<div class="header">
  <h1>trading-live-claude · dashboard</h1>
  <div class="muted">rendered {ts}</div>
</div>
{sections}
</body></html>
"""


def render_once() -> None:
    sections = [
        section_health(),
        section_overlay_scalars(),
        section_theses(),
        section_persistence(),
        section_paper_summary(),
        section_equity_curves(),
        section_allocator_bias(),
        section_validated_pool(),
        section_graph_profile(),
    ]
    html = _TEMPLATE.format(
        ts=datetime.now(UTC).isoformat(timespec="seconds"),
        sections="\n".join(sections),
    )
    REPORTS.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"[dashboard] wrote {OUT_HTML} ({len(html):,} bytes)", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", type=int, default=0,
                    help="Regenerate every N seconds (0 = one-shot). 300 is the paper poll "
                         "cadence and a reasonable default for a live view.")
    args = ap.parse_args()

    render_once()
    if args.refresh > 0:
        while True:
            time.sleep(args.refresh)
            try:
                render_once()
            except Exception as e:
                print(f"[dashboard] render failed: {e}", flush=True)


if __name__ == "__main__":
    main()
