"""Gather empirical results for the futures book on QuantConnect's cloud (its own futures data).

Runs the LEAN mirror of the paper book (integrations/lean_futures.py) for each strategy on two
universes: 'book' = micro/affordable CME contracts on the paper book's equity, 'edge' = full-size CME
benchmarks on a large account so sizing never binds and the rules themselves are measured.
Parameters are the local defaults, untuned for futures. Results: reports/qc_futures_empirical_<date>.{json,md}.

    python scripts/qc_futures_empirical.py            # smoke test, then all runs
    python scripts/qc_futures_empirical.py --smoke-only
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

logging.getLogger("httpx").setLevel(logging.WARNING)

from trading_live_claude.config import get_settings
from trading_live_claude.integrations.lean_futures import render_futures_mirror
from trading_live_claude.integrations.quantconnect import QuantConnectClient, QuantConnectError

PROJECT = "frm-futures-empirical"
TODAY = date.today()
END = (TODAY.year, TODAY.month, 1)

RUNS = [
    # name, strategy, roots, start, cash (USD ~ the CAD paper book), max open positions
    ("book-bollinger", "bollinger", ["MCL", "MHG", "ZC"], (2022, 1, 1), 73_000, 3),
    ("book-ts_momentum", "ts_momentum", ["MCL", "MHG", "ZC"], (2022, 1, 1), 73_000, 3),
    ("edge-bollinger", "bollinger", ["CL", "NG", "GC", "SI", "HG", "ZC", "ZS", "ZW"], (2012, 1, 1), 10_000_000, 8),
    ("edge-ts_momentum", "ts_momentum", ["CL", "NG", "GC", "SI", "HG", "ZC", "ZS", "ZW"], (2012, 1, 1), 10_000_000, 8),
]
SMOKE = ("smoke", "bollinger", ["MCL"], (2024, 1, 1), 73_000, 3)


def _project_id(client: QuantConnectClient) -> int:
    for p in client.list_projects():
        if p.get("name") == PROJECT:
            return int(str(p.get("projectId")))
    created = client.create_project(PROJECT, language="Py").get("projects") or [{}]
    return int(str(created[0].get("projectId", 0)))  # type: ignore[index, union-attr]


def _run(client: QuantConnectClient, project_id: int, spec: tuple, end: tuple[int, int, int]) -> dict:
    name, strategy, roots, start, cash, max_open = spec
    code = render_futures_mirror(strategy=strategy, roots=roots, start=start, end=end, cash=cash,
                                 max_open_positions=max_open)
    client.put_file(project_id, "main.py", code)
    compile_id = str(client.compile_project(project_id).get("compileId", ""))
    client.wait_for_compile(project_id, compile_id)
    bt = client.create_backtest(project_id, compile_id, f"{name} {TODAY.isoformat()}")
    backtest_id = str((bt.get("backtest") or {}).get("backtestId", ""))  # type: ignore[union-attr]
    print(f"[qc] {name}: backtest {backtest_id} running ({strategy}, {','.join(roots)}, "
          f"{start[0]}-{end[0]}, cash {cash:,})", flush=True)
    result = client.wait_for_backtest(project_id, backtest_id, timeout_seconds=3600, poll_seconds=15)
    b = result.get("backtest") or {}
    return {"name": name, "strategy": strategy, "roots": roots, "start": list(start), "end": list(end),
            "cash": cash, "backtest_id": backtest_id,
            "statistics": b.get("statistics") or {},             # type: ignore[union-attr]
            "runtime": b.get("runtimeStatistics") or {}}         # type: ignore[union-attr]


def _markdown(rows: list[dict]) -> str:
    keys = ["Compounding Annual Return", "Sharpe Ratio", "Drawdown", "Total Orders", "Win Rate",
            "Profit-Loss Ratio", "Net Profit"]
    out = [f"# Futures empirical backtests — QuantConnect ({TODAY.isoformat()})", "",
           "LEAN mirror of the paper book rules (local default parameters, untuned). No intel overlay,",
           "no allocator. `book` = affordable micros on ~CAD 100k; `edge` = full-size contracts, sizing unbound.", "",
           "| run | " + " | ".join(keys) + " |", "|---|" + "---|" * len(keys)]
    for r in rows:
        s = r["statistics"]
        out.append(f"| {r['name']} | " + " | ".join(str(s.get(k, "-")) for k in keys) + " |")
    for r in rows:
        rt = r["runtime"]
        out += ["", f"## {r['name']}", "", "| root | P&L (USD) | entries | skipped by gates |", "|---|---|---|---|"]
        for root in r["roots"]:
            out.append(f"| {root} | {rt.get(f'pnl_{root}', '-')} | {rt.get(f'entries_{root}', '-')} | "
                       f"{rt.get(f'skipped_{root}', '-')} |")
        years = sorted(k for k in rt if k.startswith("ret_"))
        if years:
            out += ["", "Year returns: " + ", ".join(f"{k[4:]} {rt[k]}" for k in years)]
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke-only", action="store_true")
    args = ap.parse_args()
    s = get_settings()
    if not s.quantconnect_user_id or not s.quantconnect_api_token:
        raise SystemExit("QUANTCONNECT_USER_ID / QUANTCONNECT_API_TOKEN missing in .env")
    with QuantConnectClient(s.quantconnect_user_id, s.quantconnect_api_token) as client:
        if not client.authenticate():
            raise SystemExit("QuantConnect authentication failed")
        project_id = _project_id(client)
        try:
            smoke = _run(client, project_id, SMOKE, (2024, 6, 1))
        except QuantConnectError as e:
            raise SystemExit(f"[qc] smoke test failed: {e}") from e
        print(f"[qc] smoke ok: orders={smoke['statistics'].get('Total Orders')} "
              f"runtime={dict(list(smoke['runtime'].items())[:4])}", flush=True)
        if args.smoke_only:
            return
        rows = []
        for spec in RUNS:
            try:
                rows.append(_run(client, project_id, spec, END))
            except QuantConnectError as e:
                print(f"[qc] {spec[0]} failed: {e}", flush=True)
                rows.append({"name": spec[0], "strategy": spec[1], "roots": spec[2], "error": str(e),
                             "statistics": {}, "runtime": {}})
    stem = Path("reports") / f"qc_futures_empirical_{TODAY.isoformat()}"
    stem.parent.mkdir(exist_ok=True)
    stem.with_suffix(".json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    stem.with_suffix(".md").write_text(_markdown(rows), encoding="utf-8")
    print(f"[qc] wrote {stem}.json and {stem}.md", flush=True)


if __name__ == "__main__":
    sys.exit(main())
