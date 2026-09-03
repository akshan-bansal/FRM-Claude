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
    classify_symbol,
)
from trading_live_claude.intel.worldmonitor import WorldMonitorClient

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
                "ann_vol": float("nan"), "sharpe": float("nan"), "sortino": float("nan"),
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
    return {"bars": int(len(closes)),
            "start": str(closes.index[0].date() if hasattr(closes.index[0], "date") else closes.index[0]),
            "end":   str(closes.index[-1].date() if hasattr(closes.index[-1], "date") else closes.index[-1]),
            "ann_ret": round(ann_ret, 4), "ann_vol": round(ann_vol, 4),
            "sharpe": round(sharpe, 3), "sortino": round(sortino, 3),
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
        # Interpret: which theses touch this symbol?
        matching = []
        for t in theses:
            try:
                imp = t.implicated_symbols() if hasattr(t, "implicated_symbols") else ()
            except Exception:
                imp = ()
            if sym in {s.upper() for s in imp}:
                matching.append(t.title)
        records.append({
            "symbol": sym, "sec_type": sec_type, "asset_class": cls,
            **stats,
            "overlay_scalar": overlay_scalar, "overlay_halt": overlay_halt,
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
        lines.append("## Theses in force")
        for t in theses:
            lines.append(f"- **{t.title}** ({getattr(t, 'severity', '?')})")
        lines.append("")
    for cls, sub in df.groupby("asset_class"):
        sub = sub.sort_values("sharpe", ascending=False)
        lines.append(f"## {cls}  (n={len(sub)}, overlay scalar={sub['overlay_scalar'].iloc[0]}, "
                      f"halt={sub['overlay_halt'].iloc[0]})")
        lines.append("| symbol | bars | ann_ret | ann_vol | sharpe | sortino | max_dd | hit_rate | theses |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---|")
        for _, r in sub.iterrows():
            lines.append(f"| {r.symbol} | {int(r.bars)} | {r.ann_ret:.2%} | {r.ann_vol:.2%} | "
                          f"{r.sharpe:.2f} | {r.sortino:.2f} | {r.max_dd:.2%} | {r.hit_rate:.2f} | "
                          f"{r.theses_implicating} |")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[sweep-ib] wrote {csv_path} and {md_path}", flush=True)


if __name__ == "__main__":
    main()
