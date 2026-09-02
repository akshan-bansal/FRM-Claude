"""Start an Interactive Brokers paper-trading session.

Same shape as ``paper_kraken.py`` — ``PaperBroker`` simulates every fill against IB's live
quotes so the real IB paper account (or live account) is untouched. IB's ``enable_live_orders``
gate stays OFF; even if it were on, PaperBroker's short-circuit fires first.

**Prerequisites at runtime:**
  1. A running TWS or IB Gateway session with the API enabled (Configure > API > Settings >
     "Enable ActiveX and Socket Clients"). Paper defaults: TWS 7497, IB Gateway 4002.
  2. ``ib_insync`` installed in the venv: ``uv add ib_insync``.
  3. IB market-data subscriptions for whatever symbols you plan to monitor (delayed data works
     for equities without a subscription; futures / bonds / L2 need paid entitlements).

What IB adds over the QT/Kraken paper sleeves the framework already runs:
  * Bonds (corporates, treasuries, munis) — QT retail has no continuous book here
  * Native futures contracts (ES, NQ, CL, GC, ZN, …) — not ETF proxies
  * Global equities + LSE / ASX / HKEX / TSE venues
  * Options with a full Greeks surface
  * L2 depth-of-book via ``broker.request_l2_book(contract)``
  * HFT-adjacent order types (VWAP, TWAP, MIDPRICE, PEG, ICEBERG) via ``broker.place_algo_order``

Only the paper-monitor path is wired here — the L2 + algo-order surfaces are available on the
``IBBroker`` instance for research scripts to use directly.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from trading_live_claude.brokers.ib import IBBroker
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.config import get_settings
from trading_live_claude.data.cache import CandleCache
from trading_live_claude.data.market import MarketData
from trading_live_claude.execution.router import Router
from trading_live_claude.monitor.live_loop import LiveMonitor, MonitorEvent
from trading_live_claude.risk.sizing import PositionSizer
from trading_live_claude.strategies import STRATEGIES


DEFAULT_SYMBOLS = ("AAPL", "MSFT", "SPY", "QQQ", "IWM")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                    help="Comma-separated symbols to monitor. Default is a US equity smoke-test set.")
    ap.add_argument("--strategy", default="bollinger",
                    help="Fallback strategy name (matched against strategies.STRATEGIES).")
    ap.add_argument("--interval", type=int, default=300,
                    help="Poll interval in seconds. IB's paper feed is real-time.")
    ap.add_argument("--paper-equity", type=float, default=100_000.0)
    ap.add_argument("--iterations", type=int, default=0,
                    help="0 = run forever; a positive N runs that many polls and stops.")
    args = ap.parse_args()

    settings = get_settings()

    # IB paper connection. If ib_insync is missing OR TWS/Gateway isn't running, this raises
    # a clear BrokerError with instructions — no silent failure at first quote.
    port = settings.ib_paper_port if settings.ib_use_paper else settings.ib_live_port
    feed = IBBroker(
        host=settings.ib_host,
        port=port,
        client_id=settings.ib_client_id,
        account=settings.ib_account or "",
        enable_live_orders=False,        # extra gate; PaperBroker also short-circuits
        readonly_market_data=True,
    )
    exec_broker = PaperBroker(feed=feed, starting_equity=args.paper_equity,
                              journal_dir=Path(settings.state_dir))
    exec_account = exec_broker.accounts()[0].number
    print(f"[ib-paper] PAPER mode. session_id={exec_broker.session_id} "
          f"starting_equity=${args.paper_equity:,.0f} account={exec_account}", flush=True)
    print(f"[ib-paper] IB feed: {settings.ib_host}:{port} (paper={settings.ib_use_paper})",
          flush=True)
    print("[ib-paper] Real IB account untouched; fills are simulated against IB live quotes.",
          flush=True)

    router = Router.build_default(
        mode="paper",
        broker=exec_broker,
        state_dir=settings.state_dir,
        cap_pct=settings.portfolio_heat_cap,
        max_drawdown_pct=settings.max_drawdown_kill_switch,
        daily_loss_limit_pct=settings.daily_loss_limit_pct,
        max_open_positions=settings.max_open_positions,
        min_ticket_usd=settings.min_ticket_usd,
    )

    market = MarketData(exec_broker, cache=CandleCache(settings.data_cache_dir))
    sizer = PositionSizer(risk_pct=settings.risk_pct_per_trade)

    sym_list = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    strat = STRATEGIES[args.strategy]()
    print(f"[ib-paper] monitoring {len(sym_list)} symbols with strategy '{args.strategy}': "
          f"{sym_list}", flush=True)

    def _emit(ev: MonitorEvent) -> None:
        state = "NEW" if ev.is_transition else f"persisting ({ev.poll_count})"
        print(f"[ib-paper] {ev.kind.upper()} {ev.symbol} @ {ev.price:.4f} "
              f"({state}) detail={ev.detail}", flush=True)

    monitor = LiveMonitor(
        broker=exec_broker,
        market=market,
        strategy=strat,
        sizer=sizer,
        router=router,
        account_number=exec_account,
        symbols=sym_list,
        interval_seconds=args.interval,
        on_event=_emit,
        account_currency="USD",
        emit_on_change_only=False,
    )
    try:
        monitor.run_forever(max_iterations=args.iterations or None)
    finally:
        feed.close()


if __name__ == "__main__":
    main()
