"""Start a 24-hour global paper book: IB equities on every configured venue + the Kraken crypto sleeve.

One PaperBroker, one Router (one kill-switch, heat budget and leverage cap) measured in
``account_currency``. Feeds: stocks via IB socket (TWS / IB Gateway), ``BASE/QUOTE`` pairs via
Kraken, each stale-guarded and converted with IB spot FX. ``SessionRouter`` queues intents for
closed venues, releases them after the open auction, and applies spread / board-lot controls.
Nothing touches a real account.

    python scripts/paper_global.py --equities "XIC.TO,AAPL,7203.T" --crypto sleeve
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
    except Exception:
        pass

from trading_live_claude.analysis.symbol_validation import (
    format_validation_banner,
    refuse_launch_on_hard_failures,
    validate_sleeve,
)
from trading_live_claude.analysis.universe import CRYPTO_SLEEVE
from trading_live_claude.brokers.base import BrokerError
from trading_live_claude.brokers.fresh import guard_feed
from trading_live_claude.brokers.fx import CurrencyNormalizingBroker, ib_spot_rates
from trading_live_claude.brokers.ib import IBBroker, require_paper_or_data_only
from trading_live_claude.brokers.kraken import KrakenBroker
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.brokers.routed import VenueRoutedFeed
from trading_live_claude.config import get_settings
from trading_live_claude.data.cache import CandleCache
from trading_live_claude.data.market import MarketData
from trading_live_claude.execution.router import Router
from trading_live_claude.execution.scheduler import MicrostructureConfig, SessionRouter
from trading_live_claude.futures import (
    FuturesBook,
    make_roller,
    overlay_class_for,
    spec_from_ib_details,
)
from trading_live_claude.intel.graph_interpret import GraphInterpreter
from trading_live_claude.intel.overlay import IntelSnapshot
from trading_live_claude.intel.routing import OverlayProvider, PersistenceGate
from trading_live_claude.intel.worldmonitor import WorldMonitorClient
from trading_live_claude.monitor.live_loop import LiveMonitor, MonitorEvent
from trading_live_claude.risk.position_cap import position_cap_for
from trading_live_claude.risk.sizing import PositionSizer
from trading_live_claude.strategies import STRATEGIES
from trading_live_claude.venues import market_open, venue_for


def _parse_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in (p for p in raw.split(",") if p.strip()):
        sym, _, name = pair.partition("=")
        out[sym.strip().upper()] = name.strip()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--equities", default="",
                    help="Comma-separated IB stock symbols with venue suffixes, e.g. 'XIC.TO,AAPL,7203.T'.")
    ap.add_argument("--crypto", default="sleeve",
                    help="'sleeve' for CRYPTO_SLEEVE, '' for none, or comma-separated Kraken pairs.")
    ap.add_argument("--strategy", default="bollinger", help="Fallback strategy for equities.")
    ap.add_argument("--strategy-map", dest="strategy_map", default="",
                    help="Per-symbol overrides, e.g. 'XIC.TO=rsi_meanrevert,7203.T=ts_momentum'.")
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--paper-equity", type=float, default=100_000.0)
    ap.add_argument("--iterations", type=int, default=0)
    ap.add_argument("--futures", default="",
                    help="Path to config/futures_universe.json from discover_futures.py; enabled rows trade.")
    ap.add_argument("--ib-port", dest="ib_port", type=int, default=None,
                    help="TWS/Gateway API port; defaults to ib_paper_port (or ib_live_port) from settings.")
    ap.add_argument("--live-data-only", dest="live_data_only", action="store_true",
                    help="Allow a live IB login as a read-only data feed (tick Read-Only API in TWS).")
    ap.add_argument("--intel", action=argparse.BooleanOptionalAction, default=True,
                    help="Overlay + graph persistence gate + graph-weighted interpret (needs WORLDMONITOR_API_KEY).")
    ap.add_argument("--persistence-polls", dest="persistence_polls", type=int, default=5)
    ap.add_argument("--graph-polls", dest="graph_polls", type=int, default=3)
    ap.add_argument("--fill-model", dest="fill_model", choices=("touch", "mid"), default="touch",
                    help="touch = buys at ask / sells at bid (default); mid = legacy mid-price fills.")
    args = ap.parse_args()

    settings = get_settings()
    numeraire = settings.account_currency

    equities = [s.strip().upper() for s in args.equities.split(",") if s.strip()]
    if args.crypto.strip().lower() == "sleeve":
        crypto = list(CRYPTO_SLEEVE)
    else:
        crypto = [s.strip().upper() for s in args.crypto.split(",") if s.strip()]
    if any("/" in s for s in equities) or any("/" not in s for s in crypto):
        raise SystemExit("--equities takes stock symbols and --crypto takes BASE/QUOTE pairs.")
    symbols = equities + crypto
    if not symbols and not args.futures:
        raise SystemExit("Nothing to trade: pass --equities, --crypto and/or --futures.")

    port = args.ib_port or (settings.ib_paper_port if settings.ib_use_paper else settings.ib_live_port)
    ib = IBBroker(host=settings.ib_host, port=port, client_id=settings.ib_client_id,
                  account=settings.ib_account or "", enable_live_orders=False,
                  readonly_market_data=True)
    try:
        ib_mode = require_paper_or_data_only(ib._require_ib().managedAccounts(),
                                             live_data_only=args.live_data_only)
    except BrokerError as e:
        ib.close()
        raise SystemExit(f"[global-paper] refusing: {e}") from e
    if ib_mode == "live-data-only":
        print("[global-paper] LIVE IB login used as a READ-ONLY data feed. Every fill is simulated in "
              "PaperBroker; IB order routing is disabled in code.", flush=True)
    kraken = KrakenBroker(enable_live_orders=False)
    routed = VenueRoutedFeed({"CRYPTO": kraken}, default=ib)

    book = FuturesBook(roll_bdays=settings.futures_roll_bdays)
    futures_rows: list[dict] = []
    if args.futures:
        futures_rows = [r for r in json.loads(Path(args.futures).read_text(encoding="utf-8"))["contracts"]
                        if r.get("enabled")]

    def refresh_futures() -> None:
        for row in futures_rows:
            spec = spec_from_ib_details(
                ib.futures_contract_details(row["root"], row["exchange"], row["currency"]),
                symbol=row["symbol"], trading_class=row.get("trading_class"))
            if spec is None:
                print(f"[global-paper] {row['symbol']}: no listed contracts; skipped", flush=True)
                continue
            book.add(spec)

    refresh_futures()
    ib.futures_contract_for = book.contract_for
    futures = sorted(book.specs)
    symbols = symbols + futures

    validations = validate_sleeve(routed, symbols)
    print(format_validation_banner(validations), flush=True)
    refuse_launch_on_hard_failures(validations)

    rates = ib_spot_rates(ib, numeraire, ttl_s=settings.fx_rate_ttl_s,
                          max_age_s=settings.fx_max_rate_age_s)
    price_feed = CurrencyNormalizingBroker(guard_feed(routed, settings), rates,
                                           multiplier_for=book.multiplier_for)
    exec_broker = PaperBroker(feed=price_feed, starting_equity=args.paper_equity,
                              journal_dir=Path(settings.state_dir), fill_model=args.fill_model)
    exec_account = exec_broker.accounts()[0].number

    inner = Router.build_default(
        mode="paper",
        broker=exec_broker,
        state_dir=settings.state_dir,
        cap_pct=settings.portfolio_heat_cap,
        max_drawdown_pct=settings.max_drawdown_kill_switch,
        daily_loss_limit_pct=settings.daily_loss_limit_pct,
        max_open_positions=settings.max_open_positions,
        min_ticket_usd=settings.min_ticket_usd,
    )
    router = SessionRouter(
        inner, exec_broker, account_number=exec_account,
        config=MicrostructureConfig(
            open_buffer_min=settings.scheduler_open_buffer_min,
            close_buffer_min=settings.scheduler_close_buffer_min,
            intent_ttl_min=settings.scheduler_intent_ttl_min,
            max_spread_bps_equity=settings.max_spread_bps_equity,
            max_spread_bps_crypto=settings.max_spread_bps_crypto,
            board_lots={k.upper(): v for k, v in settings.board_lots.items()},
        ),
        journal_path=Path(settings.state_dir) / "scheduled_intents.jsonl",
    )

    overrides = _parse_map(args.strategy_map)
    smap = {}
    for sym in symbols:
        if sym in overrides:
            smap[sym] = STRATEGIES[overrides[sym]]()
        elif sym in CRYPTO_SLEEVE:
            entry = CRYPTO_SLEEVE[sym]
            smap[sym] = STRATEGIES[entry.strategy](**dict(entry.params))
    fallback = STRATEGIES[args.strategy]()

    market = MarketData(exec_broker,
                        cache=CandleCache(Path(settings.data_cache_dir) / f"numeraire_{numeraire}"))
    inner.position_cap_pct_for = position_cap_for(settings, market)

    # Intel: the overlay provider writes every WorldMonitor read into state/intel_graph.jsonl (fills
    # already land there via PaperBroker); the persistence gate and the interpreter read it back.
    overlay_for = persistence_for = interpret_for = None
    if args.intel and settings.worldmonitor_api_key:
        classes = {s: overlay_class_for(book.specs[s]) for s in futures}

        def _snapshot() -> IntelSnapshot:
            async def _fetch() -> IntelSnapshot:
                async with WorldMonitorClient(settings.worldmonitor_api_key) as wm:
                    return await wm.snapshot()
            return asyncio.run(_fetch())

        provider = OverlayProvider(_snapshot, refresh_seconds=900.0, class_overrides=classes)
        overlay_for = provider
        persistence_for = PersistenceGate(min_polls=args.persistence_polls, refresh_seconds=300.0,
                                          class_overrides=classes)
        interpret_for = GraphInterpreter(lambda: provider.last_snapshot, min_polls=args.graph_polls)
        print(f"[global-paper] intel ON: overlay -> graph, persistence gate ({args.persistence_polls} polls), "
              f"graph-weighted interpret ({args.graph_polls} polls); futures classes "
              f"{sorted(set(classes.values()))}", flush=True)
    else:
        print("[global-paper] intel OFF" + ("" if args.intel else " (--no-intel)") +
              ("" if settings.worldmonitor_api_key else " (no WORLDMONITOR_API_KEY)"), flush=True)
    venues = sorted({venue_for(s)[0].code for s in symbols})
    print(f"[global-paper] PAPER. session_id={exec_broker.session_id} "
          f"equity={args.paper_equity:,.0f} {numeraire} fills={args.fill_model} venues={venues}",
          flush=True)
    print(f"[global-paper] {len(equities)} equities via IB {settings.ib_host}:{port}, "
          f"{len(crypto)} crypto pairs via Kraken; FX via IB spot. Real accounts untouched.",
          flush=True)

    roller = make_roller(
        book, exec_broker, router, account_number=exec_account,
        is_tradeable=lambda s: venue_for(s)[0].tradeable(
            open_buffer_min=settings.scheduler_open_buffer_min,
            close_buffer_min=settings.scheduler_close_buffer_min))
    last_refresh = [time.monotonic()]

    def roll(**gates: float) -> None:
        # IB liquidHours only cover about a week ahead; re-pull specs twice a day.
        if time.monotonic() - last_refresh[0] > 12 * 3600:
            refresh_futures()
            last_refresh[0] = time.monotonic()
        roller(**gates)

    if futures:
        print(f"[global-paper] futures: {', '.join(f'{s}={book.current[s].local_symbol}' for s in futures if s in book.current)}",
              flush=True)

    def _emit(ev: MonitorEvent) -> None:
        state = "NEW" if ev.is_transition else f"persisting ({ev.poll_count})"
        print(f"[global-paper] {ev.kind.upper()} {ev.symbol} @ {ev.price:.4f} ({state})", flush=True)

    monitor = LiveMonitor(
        broker=exec_broker,
        market=market,
        strategy=fallback,
        sizer=PositionSizer(risk_pct=settings.risk_pct_per_trade),
        router=router,  # type: ignore[arg-type]
        account_number=exec_account,
        symbols=symbols,
        interval_seconds=args.interval,
        on_event=_emit,
        account_currency=numeraire,
        emit_on_change_only=False,
        strategy_map=smap,
        risk_model=settings.risk_model,
        heat_aggregation=settings.heat_aggregation,
        market_open_for=market_open,
        corr_lead_lag=settings.corr_lead_lag,
        roll_futures=roll if futures else None,
        overlay_for=overlay_for,
        interpret_for=interpret_for,
        persistence_for=persistence_for,
    )
    try:
        monitor.run_forever(max_iterations=args.iterations or None)
    finally:
        ib.close()
        kraken.close()


if __name__ == "__main__":
    main()
