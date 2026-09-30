"""Start a 24-hour global paper book: IB futures, overseas equities and/or Kraken crypto, one currency.

One PaperBroker, one Router (one kill-switch, heat budget and leverage cap) measured in the book
``--numeraire``. Feeds: futures via IB socket (TWS / IB Gateway), ``BASE/QUOTE`` pairs via Kraken,
each stale-guarded. ``SessionRouter`` queues intents for closed venues, releases them after the
open auction, and applies spread / board-lot controls. Nothing touches a real account.

Per the desk venue split, IB carries futures plus overseas-listed equities (.L, .AX, .T, .HK);
US/TSX equities trade on Questrade and are refused. FX is not sourced from IB, so every instrument
must already be in the numeraire: run one book per currency. Futures rows in a different currency
from the numeraire are skipped, not refused, so the same universe file serves every book. The
futures contract multiplier (contract size, not FX) is still applied.

Exchange hopping = several of these books running side by side, each on its own ``--client-id``,
each idle while its venues are closed (``SessionRouter`` queues intents until the open):

    python scripts/paper_global.py --crypto "" --futures config/futures_universe.json --numeraire USD --client-id 51
    python scripts/paper_global.py --crypto "" --futures config/futures_universe.json --numeraire JPY         --equities 1306.T --paper-equity 15000000 --min-ticket 15000 --client-id 52

Stop a book cleanly with ``touch state/STOP_<session_id>`` (printed at boot): it exits within one
poll and flattens through the Router. A process-manager kill skips the flatten.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
import time
from datetime import UTC, datetime
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
from trading_live_claude.audit import Ledger
from trading_live_claude.brokers.base import BrokerError
from trading_live_claude.brokers.fresh import guard_feed
from trading_live_claude.brokers.fx import CurrencyNormalizingBroker, FxRates
from trading_live_claude.brokers.ib import IBBroker, require_paper_or_data_only
from trading_live_claude.brokers.kraken import KrakenBroker
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.brokers.routed import VenueRoutedFeed
from trading_live_claude.config import get_settings
from trading_live_claude.data.cache import CandleCache
from trading_live_claude.data.market import MarketData
from trading_live_claude.desk_policy import (
    VenuePolicyError,
    assert_ib_no_questrade_equities,
    assert_single_currency,
    require_explicit_book_sizing,
)
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
                    help="Comma-separated overseas listings via IB: .L (LSE), .AX (ASX), .T (Tokyo), "
                         ".HK (Hong Kong; needs a board_lots entry). US/TSX names are refused — they "
                         "trade on Questrade.")
    ap.add_argument("--crypto", default="sleeve",
                    help="'sleeve' for CRYPTO_SLEEVE, '' for none, or comma-separated Kraken pairs.")
    ap.add_argument("--strategy", default="bollinger",
                    help="Fallback strategy for futures (crypto uses its CRYPTO_SLEEVE strategy).")
    ap.add_argument("--strategy-map", dest="strategy_map", default="",
                    help="Per-symbol overrides, e.g. '/MCL=ts_momentum,/GC=bollinger'.")
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--paper-equity", type=float, default=None,
                    help="Starting equity in the book currency. Default 100,000 for CAD/USD books; "
                         "required for any other numeraire.")
    ap.add_argument("--min-ticket", dest="min_ticket", type=float, default=None,
                    help="Router minimum ticket in the book currency. Default min_ticket_usd from "
                         "settings for CAD/USD books; required for any other numeraire.")
    ap.add_argument("--client-id", dest="client_id", type=int, default=None,
                    help="IB API client id (default ib_client_id from settings). Every book sharing "
                         "one TWS needs its own, or IB drops the second connection.")
    ap.add_argument("--resume-session", default="",
                    help="Continue an earlier paper session's book from the state/ journals instead "
                         "of starting flat (same --paper-equity as the original). Refuses to start "
                         "if the journals disagree. Pair with --no-flatten-on-exit on the session "
                         "you stop for a restart.")
    ap.add_argument("--flatten-on-exit", dest="flatten_on_exit",
                    default=True, action=argparse.BooleanOptionalAction,
                    help="Close every open position through the Router when the loop exits. ON by "
                         "default. Stop a background book with state/STOP_<session_id>; a "
                         "process-manager kill skips the flatten. A position whose venue is closed "
                         "at exit is queued by the SessionRouter, not filled, and is reported.")
    ap.add_argument("--numeraire", default=None,
                    help="Book currency (default: account_currency from settings). FX is off IB, so "
                         "every instrument must already be in this currency (e.g. USD for CME micros).")
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
    ap.add_argument("--audit-ledger", dest="audit_ledger",
                    default=True, action=argparse.BooleanOptionalAction,
                    help="Write the hash-chained audit ledger under state/ledger/ alongside the "
                         "existing journals (AUDIT_LEDGER_SCOPE.md), on stream \"global\" so this "
                         "book's chain is independent of the other books running beside it. ON by "
                         "default; additive and non-strict, so a ledger write failure is logged "
                         "rather than raised and cannot stop a session.")
    ap.add_argument("--fill-model", dest="fill_model", choices=("touch", "mid"), default="touch",
                    help="touch = buys at ask / sells at bid (default); mid = legacy mid-price fills.")
    args = ap.parse_args()

    settings = get_settings()
    numeraire = (args.numeraire or settings.account_currency).upper()
    try:
        require_explicit_book_sizing(numeraire, args.paper_equity, args.min_ticket)
    except VenuePolicyError as e:
        raise SystemExit(f"[global-paper] refusing: {e}") from e
    paper_equity = args.paper_equity if args.paper_equity is not None else 100_000.0
    min_ticket = args.min_ticket if args.min_ticket is not None else settings.min_ticket_usd

    equities = [s.strip().upper() for s in args.equities.split(",") if s.strip()]
    try:
        # Desk venue split: US/TSX equities trade on Questrade; overseas listings may use IB.
        assert_ib_no_questrade_equities(equities)
    except VenuePolicyError as e:
        raise SystemExit(f"[global-paper] refusing: {e}") from e
    if args.crypto.strip().lower() == "sleeve":
        crypto = list(CRYPTO_SLEEVE)
    else:
        crypto = [s.strip().upper() for s in args.crypto.split(",") if s.strip()]
    if any("/" not in s for s in crypto):
        raise SystemExit("--crypto takes BASE/QUOTE pairs.")
    symbols = crypto + equities
    if not symbols and not args.futures:
        raise SystemExit("Nothing to trade: pass --crypto, --equities and/or --futures.")

    port = args.ib_port or (settings.ib_paper_port if settings.ib_use_paper else settings.ib_live_port)
    client_id = args.client_id if args.client_id is not None else settings.ib_client_id
    ib = IBBroker(host=settings.ib_host, port=port, client_id=client_id,
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
        enabled = [r for r in json.loads(Path(args.futures).read_text(encoding="utf-8"))["contracts"]
                   if r.get("enabled")]
        # One book per currency: rows in another currency belong to that currency's book.
        futures_rows = [r for r in enabled if str(r.get("currency", "")).upper() == numeraire]
        skipped = sorted(r["symbol"] for r in enabled if r not in futures_rows)
        if skipped:
            print(f"[global-paper] futures not in {numeraire}, left to their own book: "
                  f"{', '.join(skipped)}", flush=True)

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

    # FX was pulled off IB on 2026-09-14: this book no longer triangulates a mixed-currency basket
    # through IB spot pairs. Every instrument must already be in the numeraire, so run one book per
    # currency (e.g. --numeraire USD for CME micros + USD crypto). The identity rate source below
    # still carries the futures contract multiplier, which is contract size, not FX.
    try:
        assert_single_currency(symbols, numeraire)
    except VenuePolicyError as e:
        ib.close()
        raise SystemExit(f"[global-paper] refusing: {e}") from e

    # Board lots from IB contract details (Tokyo and Hong Kong vary per name). trading.yaml's
    # board_lots still wins; a failed lookup leaves the venue default, and Hong Kong's default of
    # 0 refuses the name rather than guessing.
    board_lots: dict[str, int] = {}
    for sym in equities:
        try:
            lot = ib.stock_details(sym).board_lot
        except BrokerError as e:
            print(f"[global-paper] {sym}: board lot lookup failed ({e}); venue default applies",
                  flush=True)
            continue
        if lot:
            board_lots[sym] = lot
    board_lots.update({k.upper(): v for k, v in settings.board_lots.items()})
    if board_lots:
        print(f"[global-paper] board lots: {board_lots}", flush=True)

    validations = validate_sleeve(routed, symbols)
    print(format_validation_banner(validations), flush=True)
    refuse_launch_on_hard_failures(validations)

    def _no_ib_fx(pair: str) -> float | None:
        raise BrokerError(f"FX is off IB; {pair} should not be needed in a single-currency book")

    rates = FxRates(_no_ib_fx, numeraire, ttl_s=settings.fx_rate_ttl_s,
                    max_age_s=settings.fx_max_rate_age_s)
    price_feed = CurrencyNormalizingBroker(guard_feed(routed, settings), rates,
                                           multiplier_for=book.multiplier_for)
    exec_broker = PaperBroker(feed=price_feed, starting_equity=paper_equity,
                              journal_dir=Path(settings.state_dir), fill_model=args.fill_model,
                              session_id=args.resume_session or None)
    if args.resume_session:
        try:
            restored = exec_broker.resume()
        except PaperBroker.RehydrationMismatch as e:
            ib.close()
            raise SystemExit(f"[global-paper] cannot resume session {args.resume_session}: {e}") from e
        print(f"[global-paper] RESUMED session {args.resume_session}: "
              f"{restored['fills_replayed']} fills replayed, positions "
              f"{restored['positions'] or 'none'}, cash {restored['cash']:,.2f} {numeraire}, "
              f"realized {restored['realized_pnl']:,.2f}.", flush=True)
    exec_account = exec_broker.accounts()[0].number

    # Audit ledger (AUDIT_LEDGER_SCOPE.md phase 3/7). One stream per book: these runners are
    # separate processes writing into one state/ directory, and a shared chain would interleave.
    ledger = None
    if args.audit_ledger:
        ledger = Ledger(Path(settings.state_dir) / "ledger", stream="global",
                        mode="paper", session_id=getattr(exec_broker, "session_id", None))
        print(f"[global-paper] audit ledger ON -> {ledger.path_for(datetime.now(UTC)).name} "
              f"(verify: python scripts/verify_ledger.py --stream global)", flush=True)

    inner = Router.build_default(
        mode="paper",
        broker=exec_broker,
        state_dir=settings.state_dir,
        cap_pct=settings.portfolio_heat_cap,
        max_drawdown_pct=settings.max_drawdown_kill_switch,
        daily_loss_limit_pct=settings.daily_loss_limit_pct,
        max_open_positions=settings.max_open_positions,
        min_ticket_usd=min_ticket,   # in the book currency, despite the parameter name
        ledger=ledger,
    )
    router = SessionRouter(
        inner, exec_broker, account_number=exec_account,
        config=MicrostructureConfig(
            open_buffer_min=settings.scheduler_open_buffer_min,
            close_buffer_min=settings.scheduler_close_buffer_min,
            intent_ttl_min=settings.scheduler_intent_ttl_min,
            max_spread_bps_equity=settings.max_spread_bps_equity,
            max_spread_bps_crypto=settings.max_spread_bps_crypto,
            board_lots=board_lots,
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
          f"equity={paper_equity:,.0f} {numeraire} min_ticket={min_ticket:,.0f} "
          f"fills={args.fill_model} venues={venues} ib_client_id={client_id}",
          flush=True)
    print(f"[global-paper] {len(futures)} futures via IB {settings.ib_host}:{port}, "
          f"{len(crypto)} crypto pairs via Kraken; single-currency book in {numeraire} (no IB FX). "
          f"Real accounts untouched.", flush=True)

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
        flatten_on_exit=args.flatten_on_exit,
        stop_sentinel_dir=Path(settings.state_dir),
    )
    print(f"[global-paper] graceful stop: touch {Path(settings.state_dir) / 'STOP'} "
          f"(all sessions) or {Path(settings.state_dir) / ('STOP_' + exec_broker.session_id)} "
          f"(this one). Exits within one poll and "
          f"{'flattens' if args.flatten_on_exit else 'does NOT flatten'}.", flush=True)

    def _on_signal(signum, _frame) -> None:
        print(f"[global-paper] signal {signum} — finishing poll, then "
              f"{'flattening' if args.flatten_on_exit else 'exiting'}.", flush=True)
        monitor.request_stop()

    for _sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(_sig, _on_signal)

    try:
        monitor.run_forever(max_iterations=args.iterations or None)
    finally:
        ib.close()
        kraken.close()


if __name__ == "__main__":
    main()
