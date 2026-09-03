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
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

# Windows console defaults to cp1252, which crashes on Unicode arrows / bullets in log lines
# (structlog prints an exception body that contains them). Force stdout/stderr to UTF-8 so a
# single bad log line doesn't kill the whole monitor process. errors="replace" is intentional
# — surviving with a '?' is strictly better than a UnicodeEncodeError crash mid-poll.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
    except Exception:                                                  # pragma: no cover
        pass

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
    """Daemon thread that pings CP Gateway's ``/tickle`` on a cadence and monitors session age.

    The Gateway drops the session after a few minutes of idle, so a 90s tickle keeps it warm
    between our 300s polls. Independent of that idle cap, CP Gateway itself hard-caps a session
    at ~24h even when kept tickled — after that the session dies and only a fresh browser login
    at ``https://localhost:5000`` restores it. Neither QT nor Kraken has this: their brokers
    auto-refresh their own tokens. This thread therefore also:

    * Escalates a WARN at ``warn_after_hours`` (default 20h) — enough runway to schedule a
      re-auth manually before it hard-fails mid-poll.
    * Escalates a CRITICAL at ``critical_after_hours`` (default 23h) — session is minutes away
      from expiring.
    * Escalates a CRITICAL on any tickle failure whose message looks like an auth expiry.

    ``warn_fn`` is a plain callable ``(level, message) → None`` — plumbed rather than hardcoded
    to stdout so that when the Alerter (queued gap #1) gets wired into paper_ib.py, threading it
    through to Telegram is a one-line change here.
    """

    def __init__(self, broker: IBWebBroker, interval_s: float = 90.0, *,
                 warn_after_hours: float = 20.0, critical_after_hours: float = 23.0,
                 warn_fn: "Callable[[str, str], None] | None" = None) -> None:
        super().__init__(daemon=True, name="ibweb-tickle")
        self.broker = broker
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._started_at = time.monotonic()
        self._warn_after_s = warn_after_hours * 3600.0
        self._critical_after_s = critical_after_hours * 3600.0
        self._warn_fn = warn_fn or (lambda level, msg: print(f"[ib-paper] {level} {msg}",
                                                              flush=True))
        # Ensure each escalation lands exactly once per instance (not once per tickle after the
        # threshold is crossed) so the phone doesn't get spammed every 90s for the last four hours.
        self._warn_fired = False
        self._critical_fired = False

    def stop(self) -> None:
        self._stop.set()

    def _age_hours(self) -> float:
        return (time.monotonic() - self._started_at) / 3600.0

    def _check_session_age(self) -> None:
        elapsed = time.monotonic() - self._started_at
        if not self._critical_fired and elapsed >= self._critical_after_s:
            self._critical_fired = True
            self._warn_fn("CRITICAL",
                           f"IB Web session is {self._age_hours():.1f}h old — expires imminently. "
                           f"Re-auth at https://localhost:5000 NOW to avoid mid-poll failure.")
        elif not self._warn_fired and elapsed >= self._warn_after_s:
            self._warn_fired = True
            self._warn_fn("WARN",
                           f"IB Web session is {self._age_hours():.1f}h old — CP Gateway caps at "
                           f"~24h. Plan a re-auth at https://localhost:5000 before the window "
                           f"closes.")

    def run(self) -> None:
        # Wait BEFORE the first tickle so we don't stampede against the first quote call.
        while not self._stop.wait(self.interval_s):
            try:
                self.broker.tickle()
            except Exception as e:                        # pragma: no cover
                # A tickle failure this late in the loop almost always means the session died —
                # fire a CRITICAL once so the human sees it in the alert stream. Suppress repeats
                # of the same failure so the log doesn't fill with the same line every 90s.
                msg = str(e).lower()
                if not self._critical_fired and (
                    "unauthorized" in msg or "not authenticated" in msg
                    or "401" in msg or "403" in msg
                ):
                    self._critical_fired = True
                    self._warn_fn("CRITICAL",
                                   f"IB Web tickle failed — session likely expired: {e}. "
                                   f"Re-auth at https://localhost:5000.")
                else:
                    print(f"[ib-paper] tickle failed: {e}", flush=True)
            self._check_session_age()


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
    ap.add_argument("--strategy-map", dest="strategy_map", default="",
                    help="Per-symbol strategy overrides, e.g. 'GLD=atr_channel,USO=rsi_meanrevert'. "
                         "Symbols not in the map use --strategy as fallback. Same shape as the QT "
                         "CLI 'trading signal --strategy-map'.")
    ap.add_argument("--futures", default="",
                    help="Comma-separated futures roots to add to --symbols and resolve as FUT "
                         "front-month contracts, e.g. 'ES,NQ,CL,GC,ZN'. Only meaningful for "
                         "--transport web. Without this flag those symbols would collide with "
                         "the STK tickers of the same name (ES=Eversource, CL=Colgate) and land "
                         "the wrong contract.")
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
    futures_list = [s.strip().upper() for s in args.futures.split(",") if s.strip()]
    # Register FUT sec_type overrides BEFORE the monitor starts polling, so the first quote/candle
    # call resolves against the futures front-month rather than the STK ticker of the same name.
    if futures_list:
        if args.transport != "web":
            raise SystemExit("--futures is currently wired only for --transport web (IBWebBroker). "
                             "For the socket transport, use IBBroker directly with an IBContract "
                             "carrying assetClass='future'.")
        for root in futures_list:
            feed.set_sec_type(root, "FUT")
        # Union with any --symbols the user also passed, preserving order and de-duping.
        sym_list = list(dict.fromkeys(sym_list + futures_list))
        print(f"[ib-paper] {len(futures_list)} futures roots registered as FUT front-month: "
              f"{futures_list}", flush=True)
    strat = STRATEGIES[args.strategy]()

    smap: dict[str, object] = {}
    smap_names: dict[str, str] = {}                  # parallel dict keeping the raw strategy name
    for pair in args.strategy_map.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise SystemExit(f"bad --strategy-map entry {pair!r}: expected SYMBOL=strategy")
        sym, sname = pair.split("=", 1)
        sym = sym.strip().upper()
        sname = sname.strip()
        if sname not in STRATEGIES:
            raise SystemExit(f"--strategy-map: unknown strategy {sname!r}. "
                             f"Known: {sorted(STRATEGIES)}")
        smap[sym] = STRATEGIES[sname]()
        smap_names[sym] = sname
    if smap:
        print(f"[ib-paper] monitoring {len(sym_list)} symbols; per-symbol strategy map:", flush=True)
        for s in sym_list:
            print(f"           {s:8s} <- {smap_names.get(s, args.strategy + ' (fallback)')}",
                  flush=True)
    else:
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
        strategy_map=smap or None,
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
