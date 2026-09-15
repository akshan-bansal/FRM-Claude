"""Read-only discovery of which commodity futures this IB login can see, and whether they fit the book.

Finds contracts by name search plus explicit CME-group probes, pulls IB contract details (exchange,
currency, multiplier, price magnifier, listed months, liquid hours) and a delayed quote, then writes
``config/futures_universe.json`` for ``paper_global.py --futures``. Review it before launching;
exchange codes are whatever IB reports, never assumed. No orders, no account changes.

    python scripts/discover_futures.py --port 7497
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
    except Exception:
        pass

import pandas as pd
from ib_insync import Contract

from trading_live_claude.brokers.base import BrokerError
from trading_live_claude.brokers.fx import ib_spot_rates
from trading_live_claude.brokers.ib import IBBroker, require_paper_or_data_only
from trading_live_claude.config import get_settings
from trading_live_claude.futures import spec_from_ib_details
from trading_live_claude.risk.position_cap import VolScaledPositionCap, annualised_vol

GROUPS: dict[str, dict[str, list]] = {
    "us": {
        "probes": [("CL", "NYMEX"), ("MCL", "NYMEX"), ("NG", "NYMEX"), ("GC", "COMEX"),
                   ("MGC", "COMEX"), ("SI", "COMEX"), ("SIL", "COMEX"), ("HG", "COMEX"),
                   ("MHG", "COMEX"), ("ZC", "CBOT"), ("ZS", "CBOT"), ("ZW", "CBOT")],
        "search": [],
    },
    "ice_europe": {"probes": [], "search": ["brent", "gasoil", "london cocoa", "robusta", "white sugar"]},
    "asia": {"probes": [], "search": ["iron ore", "osaka gold", "platinum", "rubber"]},
    "canada": {"probes": [], "search": ["canola"]},
}
_TARGET_EXCHANGES = {
    "ice_europe": ("IPE", "ICE", "ICEEU", "ICEEUSOFT", "ENDEX"),
    "asia": ("SGX", "OSE", "OSE.JPN", "JPX", "TOCOM"),
    "canada": ("ICEUS", "NYBOT", "ICECA", "WCE"),
}


def _search(ib, term: str, allowed: tuple[str, ...]) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for desc in ib.reqMatchingSymbols(term) or []:
        if "FUT" not in (desc.derivativeSecTypes or []):
            continue
        exch = (desc.contract.primaryExchange or desc.contract.exchange or "").upper()
        if not allowed or any(exch.startswith(a) for a in allowed):
            found.add((desc.contract.symbol, exch))
    return found


_DATA_TYPE = {1: "live", 2: "frzn", 3: "dlyd", 4: "dlyd"}


def _daily_closes(ib, contract) -> list[float]:
    try:
        bars = ib.reqHistoricalData(contract, endDateTime="", durationStr="4 M", barSizeSetting="1 day",
                                    whatToShow="TRADES", useRTH=False, formatDate=1)
    except Exception:
        return []
    return [float(b.close) for b in bars or [] if b.close and b.close > 0]


def _price(ib, contract, closes: list[float]) -> tuple[float | None, str]:
    """A price good enough to size one contract, and where it came from."""
    try:
        (t,) = ib.reqTickers(contract)
        px = t.marketPrice()
        if px and px == px and px > 0:
            return float(px), _DATA_TYPE.get(int(getattr(t, "marketDataType", 0) or 0), "snap")
    except Exception:
        pass
    return (closes[-1], "hist") if closes else (None, "-")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--groups", default="us,ice_europe,asia,canada")
    ap.add_argument("--paper-equity", type=float, default=100_000.0)
    ap.add_argument("--out", default="config/futures_universe.json")
    ap.add_argument("--live-data-only", dest="live_data_only", action="store_true",
                    help="Allow a live TWS login as a read-only data feed (tick Read-Only API in TWS).")
    args = ap.parse_args()

    settings = get_settings()
    port = args.port or (settings.ib_paper_port if settings.ib_use_paper else settings.ib_live_port)
    broker = IBBroker(host=settings.ib_host, port=port, client_id=97, enable_live_orders=False,
                      readonly_market_data=True)
    ib = broker._require_ib()
    ib.RequestTimeout = 15                      # a missing data permission must not hang the whole run
    try:
        mode = require_paper_or_data_only(ib.managedAccounts(), live_data_only=args.live_data_only)
    except BrokerError as e:
        broker.close()
        raise SystemExit(f"[discover] refusing: {e}") from e
    if mode == "live-data-only":
        print("[discover] LIVE login used as a read-only market-data feed. This script places no orders.")
    ib.reqMarketDataType(3)                     # delayed data if the paper login lacks subscriptions
    rates = ib_spot_rates(broker, settings.account_currency)

    candidates: dict[str, set[tuple[str, str]]] = {}
    for group in (g.strip() for g in args.groups.split(",") if g.strip()):
        cfg = GROUPS[group]
        found = set(cfg["probes"])
        for term in cfg["search"]:
            try:
                found |= _search(ib, term, _TARGET_EXCHANGES.get(group, ()))
            except Exception as e:
                print(f"[discover] search '{term}' failed: {e}", flush=True)
        candidates[group] = found

    cap_rule = VolScaledPositionCap(lambda _s: None, base_pct=settings.max_position_notional_pct,
                                    ref_vol=settings.position_cap_ref_vol,
                                    floor_pct=settings.position_cap_floor_pct,
                                    ceiling_pct=settings.position_cap_ceiling_pct)
    universe: list[dict[str, object]] = []
    used_symbols: set[str] = set()
    print(f"{'group':<11}{'symbol':<9}{'exch':<10}{'class':<7}{'ccy':<5}{'mult':>8}{'front':>11}"
          f"{'notional/ct':>15}{'vol':>6}{'cap':>10}  src  fits?  name")
    for group, pairs in candidates.items():
        for root, exch in sorted(pairs):
            try:
                details = broker.futures_contract_details(root, exch)
            except Exception as e:
                print(f"{group:<11}{root:<9}{exch:<10}  lookup failed: {e}", flush=True)
                continue
            classes = sorted({str(getattr(d.contract, "tradingClass", "") or "") for d in details})
            if not classes:
                print(f"{group:<11}{root:<9}{exch:<10}  no listed contracts", flush=True)
                continue
            for cls in classes:
                symbol = f"/{cls or root}"
                if symbol in used_symbols:
                    symbol = f"/{cls or root}_{exch}"
                spec = spec_from_ib_details(details, symbol=symbol, trading_class=cls)
                if spec is None:
                    continue
                used_symbols.add(spec.symbol)
                row = _assess(ib, spec, rates, cap_rule, args.paper_equity, settings)
                print(f"{group:<11}{spec.symbol:<9}{spec.exchange:<10}{cls:<7}{spec.currency:<5}"
                      f"{spec.scale:>8g}{row['front']:>11}{row['notional_txt']:>15}{row['vol_txt']:>6}"
                      f"{row['cap_txt']:>10}  {row['source']:<4} {row['fits']:<5}  {spec.long_name[:36]}",
                      flush=True)
                universe.append({
                    "symbol": spec.symbol, "root": spec.root, "exchange": spec.exchange,
                    "trading_class": cls, "currency": spec.currency, "group": group,
                    "multiplier": spec.multiplier, "price_magnifier": spec.price_magnifier,
                    "long_name": spec.long_name, "front": row["front"],
                    "notional_per_contract": row["notional"], "annual_vol": row["vol"],
                    "position_cap": row["cap"], "price_source": row["source"],
                    "fits_position_cap": row["fits"] == "yes", "enabled": row["fits"] == "yes"})
    broker.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"account_currency": settings.account_currency,
                               "paper_equity": args.paper_equity, "cap_mode": "vol_scaled",
                               "contracts": universe}, indent=2), encoding="utf-8")
    print(f"\n[discover] {len(universe)} contracts written to {out}; "
          f"{sum(1 for u in universe if u['enabled'])} enabled (one contract fits its vol-scaled cap on "
          f"{args.paper_equity:,.0f} {settings.account_currency}). Review 'enabled' before launching.")


def _assess(ib, spec, rates, cap_rule, equity: float, settings) -> dict[str, object]:
    front = spec.active(date.today(), settings.futures_roll_bdays)
    out: dict[str, object] = {"front": front.local_symbol if front else "-", "notional": None,
                              "vol": None, "cap": None, "source": "-", "fits": "?",
                              "notional_txt": "no quote", "vol_txt": "-", "cap_txt": "-"}
    if front is None:
        return out
    contract = Contract(conId=front.con_id, exchange=spec.exchange)
    closes = _daily_closes(ib, contract)
    px, source = _price(ib, contract, closes)
    vol = annualised_vol(pd.Series(closes), periods_per_year=252) if len(closes) > 20 else None
    try:
        fx = rates.rate(spec.currency)
    except Exception:
        fx = None
    cap_pct = VolScaledPositionCap(lambda _s: vol, base_pct=cap_rule.base_pct, ref_vol=cap_rule.ref_vol,
                                   floor_pct=cap_rule.floor_pct, ceiling_pct=cap_rule.ceiling_pct)("x")
    cap_value = equity * cap_pct
    out.update(cap=round(cap_value, 2), cap_txt=f"{cap_value:,.0f}", source=source)
    if vol is not None:
        out.update(vol=round(vol, 4), vol_txt=f"{vol:.0%}")
    if px and fx:
        notional = px * spec.scale * fx
        out.update(notional=round(notional, 2), notional_txt=f"{notional:,.0f}",
                   fits="yes" if notional <= cap_value and vol is not None else "no")
    return out


if __name__ == "__main__":
    main()
