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
import math
import sys
from datetime import date
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
    except Exception:
        pass

from ib_insync import Contract

from trading_live_claude.brokers.fx import ib_spot_rates
from trading_live_claude.brokers.ib import IBBroker
from trading_live_claude.config import get_settings
from trading_live_claude.futures import spec_from_ib_details

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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--groups", default="us,ice_europe,asia,canada")
    ap.add_argument("--paper-equity", type=float, default=100_000.0)
    ap.add_argument("--out", default="config/futures_universe.json")
    args = ap.parse_args()

    settings = get_settings()
    port = args.port or (settings.ib_paper_port if settings.ib_use_paper else settings.ib_live_port)
    broker = IBBroker(host=settings.ib_host, port=port, client_id=97, enable_live_orders=False,
                      readonly_market_data=True)
    ib = broker._require_ib()
    accounts = ib.managedAccounts()
    if not accounts or not all(a.startswith("DU") for a in accounts):
        raise SystemExit("[discover] refusing: TWS is not logged into a paper (DU*) account.")
    ib.reqMarketDataType(3)                     # delayed data if the paper login lacks subscriptions
    rates = ib_spot_rates(broker, settings.account_currency)
    cap = args.paper_equity * settings.max_position_notional_pct

    candidates: dict[str, set[tuple[str, str]]] = {}
    for group in (g.strip() for g in args.groups.split(",") if g.strip()):
        cfg = GROUPS[group]
        found = set(cfg["probes"])
        for term in cfg["search"]:
            found |= _search(ib, term, _TARGET_EXCHANGES.get(group, ()))
        candidates[group] = found

    universe: list[dict[str, object]] = []
    seen_roots: dict[str, str] = {}
    print(f"{'group':<11}{'symbol':<9}{'exch':<10}{'ccy':<5}{'mult':>9}{'front':>10}"
          f"{'notional/ct':>16}  fits?  name")
    for group, pairs in candidates.items():
        for root, exch in sorted(pairs):
            try:
                details = broker.futures_contract_details(root, exch)
            except Exception as e:
                print(f"{group:<11}{root:<9}{exch:<10}  lookup failed: {e}")
                continue
            symbol = f"/{root}" if root not in seen_roots else f"/{root}_{exch}"
            spec = spec_from_ib_details(details, symbol=symbol)
            if spec is None:
                print(f"{group:<11}{root:<9}{exch:<10}  no listed contracts")
                continue
            seen_roots.setdefault(root, exch)
            front = spec.active(date.today(), settings.futures_roll_bdays)
            notional = math.nan
            if front is not None:
                t = ib.reqTickers(Contract(conId=front.con_id, exchange=spec.exchange))[0]
                px = t.marketPrice()
                if px and px == px and px > 0:
                    try:
                        notional = px * spec.scale * rates.rate(spec.currency)
                    except Exception:
                        notional = math.nan
            fits = "yes" if notional == notional and notional <= cap else ("?" if notional != notional else "no")
            print(f"{group:<11}{spec.symbol:<9}{spec.exchange:<10}{spec.currency:<5}{spec.scale:>9g}"
                  f"{(front.local_symbol if front else '-'):>10}"
                  f"{(f'{notional:,.0f} {settings.account_currency}' if notional == notional else 'no quote'):>16}"
                  f"  {fits:<5}  {spec.long_name[:40]}")
            universe.append({"symbol": spec.symbol, "root": spec.root, "exchange": spec.exchange,
                             "currency": spec.currency, "group": group, "multiplier": spec.multiplier,
                             "price_magnifier": spec.price_magnifier, "long_name": spec.long_name,
                             "notional_per_contract": None if notional != notional else round(notional, 2),
                             "fits_position_cap": fits == "yes", "enabled": fits == "yes"})
    broker.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"account_currency": settings.account_currency,
                               "position_cap": cap, "contracts": universe}, indent=2), encoding="utf-8")
    print(f"\n[discover] {len(universe)} contracts written to {out} "
          f"({sum(1 for u in universe if u['enabled'])} enabled: fit a {cap:,.0f} "
          f"{settings.account_currency} per-position cap). Edit 'enabled' before launching.")


if __name__ == "__main__":
    main()
