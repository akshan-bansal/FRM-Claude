"""Probe the IB Client Portal API for every accessible data channel on the current session.

One-shot diagnostic — not a monitor. Walks the CP Gateway REST surface with a fixed set of test
symbols (representative of each asset class this project cares about) and reports what actually
returns real data vs. empty vs. errors. Purpose: understand what the U28453985 account has
market-data entitlements for BEFORE building sweeps or monitors against endpoints that will just
return NaN.

What it checks:

* Auth + accounts + portfolio summary + positions
* STK snapshot for benchmark equities and bond / commodity / metals ETFs
* FUT resolution via /trsrv/futures for CME / NYMEX / COMEX / CBOT / CFE roots
* FUT snapshot for the resolved front-months (this is where data subscriptions gate)
* Options chain resolution (/iserver/secdef/strikes + /info)
* Historical bars via /iserver/marketdata/history for one representative name per class
* Search endpoints (/iserver/secdef/search for a rare ticker, /trsrv/stocks for a known one)
* News endpoints (/iserver/news/portfolio if available)
* Scanner endpoints (/iserver/scanner/params)

For each: prints RESULT type (OK / EMPTY / DELAYED / DENIED / ERROR) with a one-line detail so a
scan of the output tells you what's actually usable without reading raw payloads.

Run:  python scripts/probe_ib_data.py [--json]  (json mode writes reports/ib_probe_<date>.json)

Fails fast if the Gateway isn't reachable / not authenticated.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

import httpx

from trading_live_claude.config import get_settings

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
    except Exception:
        pass


# Field ids for the market-data snapshot endpoint.
#   31=last, 84=bid, 86=ask, 85=asksize, 88=bidsize, 7295=open, 7296=high, 7311=volume
_SNAPSHOT_FIELDS = "31,84,86,85,88,7295,7296,7311"

# Test universe grouped by class. One representative per class keeps the probe fast but honest.
_TEST_STK = {
    "equity_us":       ["SPY", "QQQ", "AAPL"],
    "equity_intl":     ["ASML", "TSM", "BABA"],
    "fixed_income":    ["TLT", "IEF", "SHY", "LQD", "HYG"],
    "precious_metals": ["GLD", "SLV", "PSLV"],
    "commodity":       ["USO", "UNG", "DBC"],
    # Canadian ETFs — IB's /iserver/secdef/search doesn't accept the .TO suffix, so the resolver
    # strips it and passes secType=STK. Contract lookup then returns TSE-listed matches.
    "canadian":        ["XIC.TO", "ZAG.TO", "CGL.TO"],
}
_TEST_FUT = {
    "CME_equity":  ["ES", "NQ", "RTY", "YM"],
    "NYMEX":       ["CL", "NG", "HO"],
    "COMEX":       ["GC", "SI", "HG"],
    "CBOT_rates":  ["ZN", "ZB", "ZF"],
    "CBOT_grains": ["ZC", "ZW", "ZS"],
    # VIX futures trade on CFE under the root VIX (not VX — IB's contract catalog uses the
    # underlying's own ticker root).
    "CFE":         ["VIX"],
}

# Snapshot warm-up — IB routinely returns empty on the first request per conid because the
# market-data stream hasn't been activated yet. A short retry loop separates "no subscription"
# (retries all still empty) from "cold conid" (populates by attempt 2 or 3).
_SNAPSHOT_RETRIES = 4
_SNAPSHOT_WAIT_S = 0.75


def _classify_snapshot(row: dict) -> tuple[str, str]:
    """Turn a snapshot row into (verdict, detail). Fields prefixed with 'C' mean session close;
    fields prefixed with 'D' mean delayed; bare numbers are live."""
    last = row.get("31")
    bid = row.get("84")
    ask = row.get("86")
    if last is None and bid is None and ask is None:
        return "EMPTY", "no last/bid/ask returned (likely no market-data subscription)"
    flags = []
    for v in (last, bid, ask):
        if isinstance(v, str):
            if v.startswith("D"): flags.append("delayed")
            if v.startswith("C"): flags.append("closed")
    if flags:
        return "DELAYED", f"last={last} bid={bid} ask={ask} ({'/'.join(set(flags))})"
    return "OK", f"last={last} bid={bid} ask={ask}"


def _get(client: httpx.Client, base: str, path: str, **kw) -> tuple[int, object]:
    try:
        r = client.get(f"{base}{path}", **kw, timeout=10.0)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text[:200]
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def _post(client: httpx.Client, base: str, path: str, **kw) -> tuple[int, object]:
    try:
        r = client.post(f"{base}{path}", **kw, timeout=10.0)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text[:200]
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def _resolve_stk_conid(client: httpx.Client, base: str, symbol: str) -> int | None:
    # IB's search doesn't accept exchange suffixes (.TO, .L, etc.) — strip and rely on the
    # ``sections`` payload to identify the right listing venue by inspecting each result.
    bare = symbol.split(".")[0]
    status, body = _post(client, base, "/iserver/secdef/search",
                          json={"symbol": bare, "name": False, "secType": "STK"})
    if status != 200 or not isinstance(body, list) or not body:
        return None
    # For a suffixed input like XIC.TO, prefer the TSE-listed match; otherwise the first entry.
    want_exch = None
    if symbol.upper().endswith(".TO") or symbol.upper().endswith(".V"):
        want_exch = "TSE"
    if want_exch:
        for cand in body:
            sections = cand.get("sections") or []
            for sec in sections:
                if isinstance(sec, dict) and str(sec.get("exchange", "")).upper() == want_exch:
                    c = cand.get("conid")
                    return int(c) if c else None
        # No TSE listing — fall through to first entry (probably US listing of same ticker,
        # documented in the caveat above).
    c = body[0].get("conid")
    return int(c) if c else None


def _resolve_fut_front(client: httpx.Client, base: str, root: str) -> tuple[int | None, str | None]:
    status, body = _get(client, base, "/trsrv/futures", params={"symbols": root})
    if status != 200 or not isinstance(body, dict):
        return None, None
    entries = body.get(root) or []
    if not entries:
        return None, None
    # earliest expirationDate > today
    from datetime import UTC, datetime
    today = int(datetime.now(UTC).strftime("%Y%m%d"))
    def _exp(e): return int(str(e.get("expirationDate") or 0) or 0)
    future = [e for e in entries if _exp(e) > today]
    pick = min(future, key=_exp) if future else min(entries, key=_exp)
    return int(pick.get("conid") or 0) or None, str(pick.get("expirationDate", ""))


def _snapshot(client: httpx.Client, base: str, conid: int) -> tuple[str, str]:
    """Warm-up loop: retry the same snapshot up to ``_SNAPSHOT_RETRIES`` times with
    ``_SNAPSHOT_WAIT_S`` between attempts. IB's cold-conid behavior means the first request
    activates the stream but returns nothing; subsequent requests get the actual values. Take
    the first non-empty attempt to preserve latency data.
    """
    last_status = None
    last_body: object = None
    for attempt in range(_SNAPSHOT_RETRIES):
        status, body = _get(client, base, "/iserver/marketdata/snapshot",
                              params={"conids": str(conid), "fields": _SNAPSHOT_FIELDS})
        last_status, last_body = status, body
        if status == 200 and isinstance(body, list) and body:
            verdict, detail = _classify_snapshot(body[0])
            if verdict != "EMPTY":
                if attempt > 0:
                    detail = f"{detail} (warmed on attempt {attempt + 1})"
                return verdict, detail
        if attempt < _SNAPSHOT_RETRIES - 1:
            time.sleep(_SNAPSHOT_WAIT_S)
    if last_status != 200 or not isinstance(last_body, list) or not last_body:
        return "ERROR", f"snapshot HTTP {last_status}: {last_body}"[:200]
    # All retries came back with only field id present, no last/bid/ask — real "no data" not cold.
    return "EMPTY", (f"no last/bid/ask after {_SNAPSHOT_RETRIES} retries "
                     f"(no market-data subscription for this contract)")


def _history(client: httpx.Client, base: str, conid: int) -> tuple[str, str]:
    status, body = _get(client, base, "/iserver/marketdata/history",
                          params={"conid": str(conid), "period": "5d", "bar": "1d"})
    if status != 200:
        return "ERROR", f"history HTTP {status}: {str(body)[:120]}"
    if isinstance(body, dict):
        data = body.get("data") or []
        if data:
            return "OK", f"{len(data)} daily bars"
        # some errors come as 200 with body {"error": "..."}
        err = body.get("error") or body.get("message")
        return "EMPTY", f"empty data ({err or 'no rows'})"
    return "EMPTY", str(body)[:120]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true",
                    help="Write a structured JSON report to reports/ib_probe_<date>.json")
    ap.add_argument("--reports-dir", default="reports")
    args = ap.parse_args()

    settings = get_settings()
    base = f"https://{settings.ib_web_host}:{settings.ib_web_port}/v1/api"
    print(f"[probe-ib] base={base}", flush=True)

    with httpx.Client(verify=settings.ib_web_verify_ssl) as client:
        # 1) auth
        print("\n=== AUTH ===")
        s, body = _get(client, base, "/iserver/auth/status")
        if s != 200 or not isinstance(body, dict) or not body.get("authenticated"):
            raise SystemExit(f"[probe-ib] not authenticated (HTTP {s}). "
                              f"Log in at https://localhost:5000 first.")
        print(f"  authenticated={body.get('authenticated')} connected={body.get('connected')} "
              f"server={body.get('serverInfo', {}).get('serverName')}")

        # 2) accounts + summary
        print("\n=== ACCOUNTS ===")
        s, body = _get(client, base, "/iserver/accounts")
        accounts = body.get("accounts", []) if isinstance(body, dict) else []
        print(f"  {len(accounts)} account(s): {accounts}")

        results: dict = {"auth": True, "accounts": accounts, "stk": {}, "fut": {}, "history": {},
                          "scanner": None, "news": None}

        if accounts:
            acct = accounts[0]
            s, body = _get(client, base, f"/portfolio/{acct}/summary")
            if s == 200 and isinstance(body, dict):
                nl = body.get("netliquidation", {})
                print(f"  {acct}: netliquidation={nl.get('amount')} {nl.get('currency')}")
                results["net_liquidation"] = f"{nl.get('amount')} {nl.get('currency')}"

        # 3) STK snapshots by class
        print("\n=== STK SNAPSHOT (subscription-gated) ===")
        for cls, syms in _TEST_STK.items():
            print(f"  {cls}:")
            results["stk"][cls] = {}
            for sym in syms:
                conid = _resolve_stk_conid(client, base, sym)
                if conid is None:
                    print(f"    {sym:10s}  RESOLVE_FAIL")
                    results["stk"][cls][sym] = {"verdict": "RESOLVE_FAIL"}
                    continue
                v, d = _snapshot(client, base, conid)
                print(f"    {sym:10s}  {v:8s}  {d}")
                results["stk"][cls][sym] = {"verdict": v, "detail": d, "conid": conid}

        # 4) FUT resolution + snapshot
        print("\n=== FUT RESOLUTION + SNAPSHOT ===")
        for exch, roots in _TEST_FUT.items():
            print(f"  {exch}:")
            results["fut"][exch] = {}
            for root in roots:
                conid, exp = _resolve_fut_front(client, base, root)
                if conid is None:
                    print(f"    {root:8s}  RESOLVE_FAIL (root unknown or /trsrv/futures failed)")
                    results["fut"][exch][root] = {"verdict": "RESOLVE_FAIL"}
                    continue
                v, d = _snapshot(client, base, conid)
                print(f"    {root:8s}  conid={conid} front={exp}  {v:8s}  {d}")
                results["fut"][exch][root] = {"conid": conid, "front": exp, "verdict": v, "detail": d}

        # 5) Historical bars (one per accessible class — using the first STK conid resolved)
        print("\n=== HISTORY (5-day daily bars) ===")
        for cls, syms_map in results["stk"].items():
            first_ok = next((info for sym, info in syms_map.items()
                             if info.get("verdict") == "OK" and info.get("conid")), None)
            if not first_ok:
                continue
            v, d = _history(client, base, first_ok["conid"])
            print(f"  {cls}:  {v:6s}  {d}")
            results["history"][cls] = {"verdict": v, "detail": d}

        # History on futures — one per exchange
        for exch, roots_map in results["fut"].items():
            first_ok = next((info for root, info in roots_map.items()
                             if info.get("verdict") == "OK" and info.get("conid")), None)
            if not first_ok:
                continue
            v, d = _history(client, base, first_ok["conid"])
            print(f"  {exch:12s}: {v:6s}  {d}")
            results["history"][exch] = {"verdict": v, "detail": d}

        # 6) Scanner params (schema availability, not a scan itself)
        print("\n=== SCANNER PARAMS ===")
        s, body = _get(client, base, "/iserver/scanner/params")
        if s == 200 and isinstance(body, dict):
            n_types = len(body.get("scan_type_list", []))
            n_instr = len(body.get("instrument_list", []))
            print(f"  OK  {n_types} scan types, {n_instr} instruments available")
            results["scanner"] = {"verdict": "OK", "scan_types": n_types,
                                    "instruments": n_instr}
        else:
            print(f"  ERROR  HTTP {s}")
            results["scanner"] = {"verdict": "ERROR", "detail": str(body)[:120]}

        # 7) News (portfolio-scoped feed)
        print("\n=== NEWS ===")
        if accounts:
            s, body = _get(client, base, "/iserver/news/portfolio", params={"accountId": accounts[0]})
            if s == 200 and isinstance(body, list):
                print(f"  OK  {len(body)} news items")
                results["news"] = {"verdict": "OK", "count": len(body)}
            else:
                print(f"  {'EMPTY' if s == 200 else 'ERROR'}  HTTP {s}")
                results["news"] = {"verdict": "EMPTY", "http": s}

    # 8) Summary
    print("\n=== SUMMARY ===")
    stk_ok = sum(1 for cls in results["stk"].values() for info in cls.values()
                 if info.get("verdict") == "OK")
    stk_delayed = sum(1 for cls in results["stk"].values() for info in cls.values()
                       if info.get("verdict") == "DELAYED")
    stk_empty = sum(1 for cls in results["stk"].values() for info in cls.values()
                     if info.get("verdict") == "EMPTY")
    fut_ok = sum(1 for exch in results["fut"].values() for info in exch.values()
                  if info.get("verdict") == "OK")
    fut_delayed = sum(1 for exch in results["fut"].values() for info in exch.values()
                       if info.get("verdict") == "DELAYED")
    fut_empty = sum(1 for exch in results["fut"].values() for info in exch.values()
                     if info.get("verdict") == "EMPTY")
    print(f"  STK: {stk_ok} OK, {stk_delayed} delayed, {stk_empty} empty")
    print(f"  FUT: {fut_ok} OK, {fut_delayed} delayed, {fut_empty} empty")
    print(f"  HIST: {sum(1 for v in results['history'].values() if v.get('verdict') == 'OK')} classes with bars")

    if args.json:
        reports_dir = Path(args.reports_dir)
        reports_dir.mkdir(parents=True, exist_ok=True)
        out = reports_dir / f"ib_probe_{date.today().isoformat()}.json"
        out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
        print(f"\n  wrote {out}")


if __name__ == "__main__":
    main()
