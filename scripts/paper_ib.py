"""Start an Interactive Brokers paper-trading session.

Same shape as ``paper_kraken.py`` — ``PaperBroker`` simulates every fill against IB's live
quotes so the real IB paper account is untouched. IB's ``enable_live_orders`` gate stays OFF;
even if it were on, PaperBroker's short-circuit fires first.

Two transports for the IB feed:

* ``--transport web`` (default): REST via the Client Portal Gateway (``localhost:5000/v1/api``).
  No TWS/Gateway binary needed — you run the CP Gateway Java daemon (~50 MB) and log in via
  browser once, then it holds the session cookie. Wired through ``IBWebBroker``. The CP
  Gateway times out on idle, so this script also fires a keep-alive tickle every ~90 s.
* ``--transport socket``: legacy TWS/IB Gateway socket API via ``ib_insync``. Requires TWS or
  IB Gateway running with the API enabled. Wired through ``IBBroker``. Only reason to prefer
  this today is L2 depth-of-book or algo orders (VWAP/TWAP/etc), which the Web API doesn't
  expose in the same shape.

Optional intel layers (opt-in via ``--intel-overlay``):
  * OSINT overlay class scalar trims entry conviction (equity class for IB names).
  * Interpret theses further trim entries that implicate any symbol in the monitored list.

What IB adds over the QT/Kraken paper sleeves the framework already runs:
  * Bonds (corporates, treasuries, munis) — QT retail has no continuous book here
  * Native futures contracts (ES, NQ, CL, GC, ZN, …) — not ETF proxies
  * Global equities + LSE / ASX / HKEX / TSE venues
  * Options with a full Greeks surface
  * L2 depth-of-book via ``IBBroker.request_l2_book(contract)`` (socket transport only)
  * HFT-adjacent algo orders (VWAP, TWAP, MIDPRICE, PEG, ICEBERG) via
    ``IBBroker.place_algo_order`` (socket transport only)

The L2 + algo-order surfaces are available on the underlying feed object for research scripts
to use directly — the monitor loop here uses only the ``Broker`` protocol surface.
"""
from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

from trading_live_claude.brokers.base import Broker
from trading_live_claude.brokers.ib import IBBroker
from trading_live_claude.brokers.ib_web import CPGatewayAuth, IBWebBroker
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.config import get_settings
from trading_live_claude.data.cache import CandleCache
from trading_live_claude.data.market import MarketData
from trading_live_claude.execution.router import Router
from trading_live_claude.monitor.live_loop import LiveMonitor, MonitorEvent
from trading_live_claude.risk.sizing import PositionSizer
from trading_live_claude.strategies import STRATEGIES


DEFAULT_SYMBOLS = ("AAPL", "MSFT", "SPY", "QQQ", "IWM")


class _TickleThread(threading.Thread):
    """Daemon thread that pings CP Gateway's ``/tickle`` on a cadence.

    The Gateway drops the session after a few minutes of idle. Our poll interval is 300 s by
    default, right at the edge of the safe window, so a 90 s tickle keeps the session alive
    between polls even if no positions exist (no MTM traffic).
    """

    def __init__(self, broker: IBWebBroker, interval_s: float = 90.0) -> None:
        super().__init__(daemon=True, name="ibweb-tickle")
        self.broker = broker
        self.interval_s = interval_s
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        # Wait BEFORE the first tickle so we don't stampede against the first quote call.
        while not self._stop.wait(self.interval_s):
            try:
                self.broker.tickle()
            except Exception as e:                        # pragma: no cover
                print(f"[ib-paper] tickle failed: {e}", flush=True)


def _build_ib_feed(args: argparse.Namespace, settings) -> tuple[Broker, _TickleThread | None]:
    """Construct the IB feed per --transport. Returns (feed, optional tickle thread)."""
    if args.transport == "web":
        auth = CPGatewayAuth(host=settings.ib_web_host, port=settings.ib_web_port,
                              verify_ssl=settings.ib_web_verify_ssl)
        feed = IBWebBroker(auth=auth, enable_live_orders=False)
        tickle = _TickleThread(feed, interval_s=90.0)
        print(f"[ib-paper] transport=web  auth=CPGatewayAuth  "
              f"base={auth.base_url}", flush=True)
        return feed, tickle
    if args.transport == "socket":
        port = settings.ib_paper_port if settings.ib_use_paper else settings.ib_live_port
        feed = IBBroker(host=settings.ib_host, port=port, client_id=settings.ib_client_id,
                          account=settings.ib_account or "", enable_live_orders=False,
                          readonly_market_data=True)
        print(f"[ib-paper] transport=socket  {settings.ib_host}:{port} "
              f"(paper={settings.ib_use_paper})", flush=True)
        return feed, None
    raise SystemExit(f"unknown transport: {args.transport!r}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                    help="Comma-separated symbols to monitor. Default is a US equity smoke-test set.")
    ap.add_argument("--strategy", default="bollinger",
                    help="Fallback strategy name (matched against strategies.STRATEGIES).")
    ap.add_argument("--transport", default="web", choices=("web", "socket"),
                    help="How to reach IB. 'web' hits the Client Portal Gateway REST API "
                         "(default). 'socket' uses TWS/IB Gateway via ib_insync.")
    ap.add_argument("--interval", type=int, default=300,
                    help="Poll interval in seconds. IB's paper feed is real-time.")
    ap.add_argument("--paper-equity", type=float, default=100_000.0)
    ap.add_argument("--iterations", type=int, default=0,
                    help="0 = run forever; a positive N runs that many polls and stops.")
    ap.add_argument("--intel-overlay/--no-intel-overlay", dest="intel_overlay",
                    default=False, action=argparse.BooleanOptionalAction,
                    help="Wire the OSINT overlay (class scalar) + interpret entry-filter into "
                         "sizing. Needs WORLDMONITOR_API_KEY. Same effect it has on the QT and "
                         "Kraken monitors.")
    args = ap.parse_args()

    settings = get_settings()
    feed, tickle = _build_ib_feed(args, settings)

    exec_broker = PaperBroker(feed=feed, starting_equity=args.paper_equity,
                              journal_dir=Path(settings.state_dir))
    exec_account = exec_broker.accounts()[0].number
    print(f"[ib-paper] PAPER mode. session_id={exec_broker.session_id} "
          f"starting_equity=${args.paper_equity:,.0f} account={exec_account}", flush=True)
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

    # Optional OSINT overlay + interpret filter — same as QT paper's --intel-overlay path.
    overlay_for = None
    interpret_for = None
    if args.intel_overlay:
        if not settings.worldmonitor_api_key:
            raise SystemExit("--intel-overlay needs WORLDMONITOR_API_KEY. Set it in .env or drop the flag.")
        import asyncio as _asyncio

        from trading_live_claude.intel.interpret import interpret as _interpret
        from trading_live_claude.intel.routing import OverlayProvider
        from trading_live_claude.intel.worldmonitor import WorldMonitorClient

        def _snapshot():
            async def _f():
                async with WorldMonitorClient(settings.worldmonitor_api_key) as wm:
                    return await wm.snapshot()
            return _asyncio.run(_f())

        overlay_provider = OverlayProvider(_snapshot, refresh_seconds=900.0)
        overlay_for = overlay_provider

        def _interpret_current():
            snap = overlay_provider.last_snapshot
            return _interpret(snap) if snap is not None else []
        interpret_for = _interpret_current
        print("[ib-paper] intel overlay ON (refresh every 900s; de-risk + halt gate).",
              flush=True)
        print("[ib-paper] interpret entry-filter ON (theses trim conviction; advisory only).",
              flush=True)

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
        overlay_for=overlay_for,
        interpret_for=interpret_for,
    )

    if tickle is not None:
        tickle.start()
    try:
        monitor.run_forever(max_iterations=args.iterations or None)
    finally:
        if tickle is not None:
            tickle.stop()
        if hasattr(feed, "close"):
            feed.close()


if __name__ == "__main__":
    main()
