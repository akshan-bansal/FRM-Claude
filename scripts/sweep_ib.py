"""One-shot IB sweep: pull history for a bond / commodity / precious-metals / futures universe,
compute base statistics, and layer the OSINT overlay + interpret verdict on every name.

Not a monitor — a research pass that runs, writes ``reports/ib_sweep_YYYY-MM-DD.{csv,md}``, and
exits. Needs the CP Gateway auth'd at ``https://localhost:5000`` before it runs; without an active
session every candles() call returns 500 and the row falls out.

What each row carries:

* symbol, sec_type, asset_class (as resolved by ``intel.routing.classify_symbol``)
* base statistics over the fetched window: annualized return, annualized volatility, Sharpe,
  Sortino, max drawdown, hit rate (fraction of up-days), start / end date, bars
* overlay scalar for its class (from a live OSINT snapshot), and ``halt_new_entries``
* interpret theses that name this symbol in their implicated set — one row per matching
  thesis, comma-joined for the CSV output

Run:  python scripts/sweep_ib.py [--symbols TLT,IEF,...] [--futures ES,NQ,CL,GC,ZN]
                                 [--years 2] [--tag YYYY-MM-DD]

Defaults cover the full bond + commodity + precious-metals ETF universe declared in
``intel/routing.py``, plus the 5 default futures roots (ES/NQ/CL/GC/ZN).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from trading_live_claude.brokers.ib_web import CPGatewayAuth, IBWebBroker
from trading_live_claude.config import get_settings
from trading_live_claude.intel.interpret import interpret
from trading_live_claude.intel.overlay import RiskOverlay
from trading_live_claude.intel.routing import (
    _FIXED_INCOME_SYMBOLS,
    _PRECIOUS_METALS_SYMBOLS,
    _COMMODITY_SYMBOLS,
    PersistenceGate,
    classify_symbol,
)
from trading_live_claude.intel.worldmonitor import WorldMonitorClient
from trading_live_claude.intel.overlay import IntelSnapshot as _IntelSnapshot

# Windows console defaults to cp1252 and crashes on Unicode in log lines. Match paper_ib.py.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
    except Exception:
        pass


DEFAULT_FUTURES = ("ES", "NQ", "CL", "GC", "ZN")


def _default_universe() -> list[str]:
    """Bonds + commodities + precious metals from the routing taxonomy. Deduped, sorted."""
    return sorted(set(_FIXED_INCOME_SYMBOLS) | set(_PRECIOUS_METALS_SYMBOLS) | set(_COMMODITY_SYMBOLS))


def _base_stats(closes: pd.Series) -> dict[str, float]:
    """Base per-symbol statistics from a daily close series. Empty series → zeros / NaN."""
    if closes is None or len(closes) < 2:
        return {"bars": 0, "start": None, "end": None, "ann_ret": float("nan"),
                "ann_vol": float("nan"), "sortino_over_dd": float("nan"),
                "sortino": float("nan"), "sharpe": float("nan"),
                "max_dd": float("nan"), "hit_rate": float("nan")}
    r = closes.pct_change().dropna()
    ann_ret = float(r.mean() * 252.0)
    ann_vol = float(r.std(ddof=0) * (252.0 ** 0.5))
    sharpe = ann_ret / ann_vol if ann_vol > 1e-12 else 0.0
    downside = r[r < 0]
    down_vol = float(downside.std(ddof=0) * (252.0 ** 0.5)) if not downside.empty else 0.0
    sortino = ann_ret / down_vol if down_vol > 1e-12 else 0.0
    equity = (1.0 + r).cumprod()
    roll_max = equity.cummax()
    max_dd = float((equity / roll_max - 1.0).min())
    hit_rate = float((r > 0).mean())
    # Primary objective across the codebase (tune.py, sweep_universe.py, walk_forward_*) is
    # sortino_over_dd = Sortino / |max_dd| — a downside-only Sharpe divided by peak drawdown,
    # so a strategy that got its Sharpe from a single big drawdown gets scored down. Kept here
    # as the rank column for consistency with every other sweep in the project.
    sortino_over_dd = (sortino / abs(max_dd)) if abs(max_dd) > 1e-12 else 0.0
    return {"bars": int(len(closes)),
            "start": str(closes.index[0].date() if hasattr(closes.index[0], "date") else closes.index[0]),
            "end":   str(closes.index[-1].date() if hasattr(closes.index[-1], "date") else closes.index[-1]),
            "ann_ret": round(ann_ret, 4), "ann_vol": round(ann_vol, 4),
            "sortino_over_dd": round(sortino_over_dd, 3),
            "sortino": round(sortino, 3), "sharpe": round(sharpe, 3),
            "max_dd": round(max_dd, 4), "hit_rate": round(hit_rate, 3)}


def _fetch_candles(broker: IBWebBroker, symbol: str, years: float) -> pd.Series | None:
    """Pull ~years of daily candles from IB; return close series indexed by ts. None on error."""
    end = datetime.now(UTC)
    start = end - timedelta(days=int(years * 365))
    try:
        rows = broker.candles(symbol, start, end, interval="OneDay")
    except Exception as e:
        print(f"[sweep-ib] {symbol}: fetch failed ({type(e).__name__}: {e})", flush=True)
        return None
    if not rows:
        print(f"[sweep-ib] {symbol}: no bars returned (chart data not yet activated?)", flush=True)
        return None
    df = pd.DataFrame([{"t": r.start, "close": float(r.close)} for r in rows])
    df.set_index("t", inplace=True)
    return df["close"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(_default_universe()),
                    help="Comma-separated ETF symbols. Default is the full bond + commodity + "
                         "precious-metals universe declared in intel/routing.py.")
    ap.add_argument("--futures", default=",".join(DEFAULT_FUTURES),
                    help="Comma-separated futures roots. Resolved as FUT front-month via IB's "
                         "/trsrv/futures. Default: ES,NQ,CL,GC,ZN.")
    ap.add_argument("--years", type=float, default=2.0,
                    help="History window in years. Longer is honest but slower — IB rate-limits "
                         "per symbol, so full universe × 5y adds real wall time.")
    ap.add_argument("--tag", default=date.today().isoformat(),
                    help="Suffix for reports/ib_sweep_<tag>.{csv,md}. Default: today's ISO date.")
    ap.add_argument("--reports-dir", default="reports",
                    help="Where to write the CSV + markdown outputs.")
    ap.add_argument("--persistence-min-polls", type=int, default=5,
                    help="PersistenceGate threshold — a symbol whose overlay class has any exposed "
                         "graph domain elevated across at least this many consecutive polls in "
                         "state/intel_graph.jsonl gets flagged persistence_halt=True in the report. "
                         "Same 5-poll default as the tier 3 wiring on LiveMonitor.")
    ap.add_argument("--history-window", type=int, default=20,
                    help="How many recent rows of state/intel_overlay.jsonl to read for the "
                         "historical-overlay + interpret-history section (per-class mean/min "
                         "scalar, halt count, and per-thesis poll count over the window).")
    args = ap.parse_args()

    settings = get_settings()
    reports_dir = Path(args.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)

    # --- IB connection -----------------------------------------------------------------------
    auth = CPGatewayAuth(host=settings.ib_web_host, port=settings.ib_web_port,
                          verify_ssl=settings.ib_web_verify_ssl)
    broker = IBWebBroker(auth=auth, enable_live_orders=False)
    print(f"[sweep-ib] transport=web  base={auth.base_url}", flush=True)
    # Fail fast if the Gateway isn't authenticated — every candles() call would 500 otherwise.
    try:
        broker.accounts()
    except Exception as e:
        raise SystemExit(f"[sweep-ib] Gateway not reachable / not authenticated: {e}\n"
                         f"  Log in at https://localhost:5000 before running.") from e

    sym_list = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    fut_list = [s.strip().upper() for s in args.futures.split(",") if s.strip()]
    for root in fut_list:
        broker.set_sec_type(root, "FUT")
    all_syms = sym_list + fut_list
    print(f"[sweep-ib] {len(sym_list)} ETFs + {len(fut_list)} futures = {len(all_syms)} names",
          flush=True)

    # --- journal → sweep: persistence gate reads state/intel_graph.jsonl -----------------
    # Refresh cadence must be LARGE, not zero — refresh_seconds=0 forces a re-read on every call
    # (elapsed < 0 is never true). One read at construction is enough for a sweep that completes
    # in bounded time.
    gate = PersistenceGate(min_polls=args.persistence_min_polls, refresh_seconds=3600.0)
    gate.refresh(force=True)
    print(f"[sweep-ib] persistence gate: min_polls={args.persistence_min_polls}, "
          f"per-domain runs={gate._persistence_by_domain or {}}", flush=True)

    # --- journal → sweep: historical overlay + interpret ---------------------------------
    # Read the last N rows of state/intel_overlay.jsonl, rebuild each snapshot, and compute:
    #   * per-class scalar mean / min / halt count over the window (how conservative has this
    #     class been on average, and did it halt at all recently?)
    #   * per-thesis poll count over the window (which theses have been persistent across polls,
    #     not just fired once) — the theses journal is NOT written to disk, so this reconstructs
    #     it from the persisted snapshot fields, which is honest and cheap (interpret is pure).
    from collections import Counter

    from trading_live_claude.intel.interpret import interpret as _interpret_fn

    overlay_history_path = Path("state/intel_overlay.jsonl")
    history_rows: list[dict] = []
    if overlay_history_path.exists():
        try:
            all_rows = overlay_history_path.read_text(encoding="utf-8").splitlines()
            history_rows = [json.loads(l) for l in all_rows[-args.history_window:] if l.strip()]
        except Exception as e:
            print(f"[sweep-ib] history read failed: {e}", flush=True)

    class_hist_scalar: dict[str, list[float]] = {}
    class_halt_count: dict[str, int] = {}
    thesis_poll_count: Counter[str] = Counter()
    for row in history_rows:
        decs = row.get("decisions") or {}
        for cls, dec in decs.items():
            if isinstance(dec, dict):
                s = dec.get("scalar")
                if isinstance(s, (int, float)):
                    class_hist_scalar.setdefault(cls, []).append(float(s))
                if dec.get("halt"):
                    class_halt_count[cls] = class_halt_count.get(cls, 0) + 1
        # Reconstruct the snapshot and rerun interpret to derive which theses had fired.
        snap_dict = row.get("snapshot") or {}
        try:
            snap = _IntelSnapshot(**snap_dict)
            fired = _interpret_fn(snap)
            for t in fired:
                thesis_poll_count[t.name] += 1
        except Exception:
            pass                           # a legacy row shape isn't worth breaking the sweep

    class_hist: dict[str, dict] = {}
    for cls, scalars in class_hist_scalar.items():
        class_hist[cls] = {
            "mean": round(sum(scalars) / len(scalars), 4),
            "min": round(min(scalars), 4),
            "halts": int(class_halt_count.get(cls, 0)),
            "polls": len(scalars),
        }
    print(f"[sweep-ib] history window: {len(history_rows)} polls read; "
          f"{len(thesis_poll_count)} distinct theses observed", flush=True)

    # --- overlay + interpret snapshot --------------------------------------------------------
    overlay = RiskOverlay()
    theses: list = []
    class_decisions: dict = {}
    if settings.worldmonitor_api_key:
        async def _snap():
            async with WorldMonitorClient(settings.worldmonitor_api_key) as wm:
                return await wm.snapshot()
        snap = asyncio.run(_snap())
        class_decisions = overlay.evaluate(snap)
        theses = interpret(snap)
        print(f"[sweep-ib] overlay + interpret snapshot taken; {len(theses)} theses fired",
              flush=True)
    else:
        print("[sweep-ib] WORLDMONITOR_API_KEY not set — overlay + interpret columns will be blank",
              flush=True)

    # --- per-symbol sweep --------------------------------------------------------------------
    records: list[dict] = []
    for sym in all_syms:
        sec_type = "FUT" if sym in fut_list else "STK"
        cls = "future" if sec_type == "FUT" else classify_symbol(sym)
        print(f"[sweep-ib] {sym:>10}  sec_type={sec_type:3s}  class={cls}", flush=True)
        closes = _fetch_candles(broker, sym, years=args.years)
        stats = _base_stats(closes) if closes is not None else _base_stats(None)
        dec = class_decisions.get(cls)
        overlay_scalar = round(dec.scalar, 4) if dec is not None else None
        overlay_halt = dec.halt_new_entries if dec is not None else None
        # Interpret: which theses touch this symbol? Thesis.exemplars() lists the ticker
        # exemplars each thesis implies via its themes.
        matching = []
        for t in theses:
            try:
                imp = t.exemplars() if hasattr(t, "exemplars") else ()
            except Exception:
                imp = ()
            if sym in {s.upper() for s in imp}:
                matching.append(t.name)
        # Per-symbol enrichments from the graph + overlay history journals
        p_halt, p_reason = gate(sym)
        hist = class_hist.get(cls, {})
        records.append({
            "symbol": sym, "sec_type": sec_type, "asset_class": cls,
            **stats,
            "overlay_scalar": overlay_scalar, "overlay_halt": overlay_halt,
            "overlay_20p_mean": hist.get("mean"),
            "overlay_20p_min": hist.get("min"),
            "overlay_20p_halts": hist.get("halts"),
            "persistence_halt": p_halt,
            "persistence_reason": p_reason,
            "theses_implicating": ", ".join(matching),
        })

    df = pd.DataFrame(records)
    csv_path = reports_dir / f"ib_sweep_{args.tag}.csv"
    md_path = reports_dir / f"ib_sweep_{args.tag}.md"
    df.to_csv(csv_path, index=False)

    # Compact markdown: one section per asset class, ranked by Sharpe within each.
    lines = [f"# IB sweep — {args.tag}", ""]
    lines.append(f"Fetched {len(df)} names ({args.years}y daily bars via IB Web). "
                  f"Overlay + interpret snapshot: {len(theses)} theses fired.")
    lines.append("")
    if theses:
        lines.append(f"## Theses in force  (with poll count over the last {len(history_rows)} polls)")
        for t in theses:
            polls_fired = thesis_poll_count.get(t.name, 0)
            marker = " 🔁 persistent" if polls_fired >= max(3, len(history_rows) // 3) else ""
            lines.append(f"- **{t.name}** ({getattr(t, 'confidence', '?')}) — "
                          f"fired in {polls_fired}/{len(history_rows)} recent polls{marker}")
        # Persistent theses NOT currently firing this snapshot are also worth naming — a
        # thesis that fired 10 of the last 20 polls but not the current one is either a real
        # regime break or a noise-driven pause.
        current_names = {t.name for t in theses}
        for name, count in thesis_poll_count.most_common():
            if name in current_names or count < max(3, len(history_rows) // 3):
                continue
            lines.append(f"- _{name}_ (not current) — fired in {count}/{len(history_rows)} "
                          f"recent polls; regime break or noise pause")
        lines.append("")

    # Per-class overlay history — the "how conservative has this class been on average" view.
    if class_hist:
        lines.append(f"## Per-class overlay history (last {len(history_rows)} polls)")
        lines.append("| class | mean scalar | min scalar | halts | polls |")
        lines.append("|---|---:|---:|---:|---:|")
        for cls, h in sorted(class_hist.items()):
            lines.append(f"| {cls} | {h['mean']:.3f} | {h['min']:.3f} | {h['halts']} | {h['polls']} |")
        lines.append("")

    # Per-domain persistence — the graph journal side.
    per_dom = gate._persistence_by_domain or {}
    if any(v > 0 for v in per_dom.values()):
        lines.append(f"## Per-domain persistence (consecutive-poll run) — threshold "
                      f"{args.persistence_min_polls}")
        lines.append("| domain | consecutive polls elevated |")
        lines.append("|---|---:|")
        for dom, run in sorted(per_dom.items(), key=lambda kv: -kv[1]):
            marker = " 🚨" if run >= args.persistence_min_polls else ""
            lines.append(f"| {dom} | {run}{marker} |")
        lines.append("")
    for cls, sub in df.groupby("asset_class"):
        # Rank by sortino_over_dd — the primary objective across the codebase (tune, sweep,
        # walk_forward_*). Sharpe stays in the table as reference but is not the sort key.
        sub = sub.sort_values("sortino_over_dd", ascending=False)
        hist_line = ""
        h = class_hist.get(cls)
        if h:
            hist_line = (f" | historical: mean={h['mean']:.2f} min={h['min']:.2f} "
                          f"halts={h['halts']}/{h['polls']}")
        lines.append(f"## {cls}  (n={len(sub)}, overlay scalar={sub['overlay_scalar'].iloc[0]}, "
                      f"halt={sub['overlay_halt'].iloc[0]}{hist_line})")
        lines.append("| symbol | bars | ann_ret | ann_vol | **sortino/DD** | sortino | sharpe | max_dd | hit_rate | persist | theses |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|")
        for _, r in sub.iterrows():
            persist = "🚨 HALT" if r.persistence_halt else ""
            lines.append(f"| {r.symbol} | {int(r.bars)} | {r.ann_ret:.2%} | {r.ann_vol:.2%} | "
                          f"**{r.sortino_over_dd:.2f}** | {r.sortino:.2f} | {r.sharpe:.2f} | "
                          f"{r.max_dd:.2%} | {r.hit_rate:.2f} | {persist} | {r.theses_implicating} |")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[sweep-ib] wrote {csv_path} and {md_path}", flush=True)

    # --- sweep → journal: persist rankings back into state/intel_graph.jsonl -----------
    # One "analysis" node per sweep run (identified by the tag), plus one ``ranked_by`` edge per
    # symbol carrying the sortino_over_dd score as weight and the rest of the row in meta. A
    # dashboard query "select ranked_by across analyses over time" then plots how a name's
    # sortino/DD trended run-over-run, which the CSV report alone can't answer.
    from trading_live_claude.intel.graph import Edge, append_edges
    analysis_id = f"ib_sweep_{args.tag}"
    as_of = datetime.now(UTC).isoformat()
    edges: list[Edge] = []
    for _, r in df.iterrows():
        # Skip rows where fetch failed — a symbol with 0 bars has no ranking to persist.
        if r.bars == 0 or pd.isna(r.sortino_over_dd):
            continue
        edges.append(Edge(
            subject=("analysis", analysis_id),
            predicate="ranked_by",
            object=("symbol", str(r.symbol)),
            weight=float(r.sortino_over_dd),
            as_of=as_of,
            meta={
                "asset_class": str(r.asset_class),
                "sec_type": str(r.sec_type),
                "sharpe": float(r.sharpe),
                "sortino": float(r.sortino),
                "ann_ret": float(r.ann_ret),
                "max_dd": float(r.max_dd),
                "bars": int(r.bars),
                "overlay_scalar": float(r.overlay_scalar) if r.overlay_scalar is not None else -1.0,
                "persistence_halt": bool(r.persistence_halt),
            },
        ))
    if edges:
        append_edges(edges)
        print(f"[sweep-ib] persisted {len(edges)} ranked_by edges to state/intel_graph.jsonl "
              f"under analysis '{analysis_id}'", flush=True)


if __name__ == "__main__":
    main()
