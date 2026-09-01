"""Start a Kraken paper-trading session over the crypto sleeve.

Runs the same LiveMonitor loop the equity ``signal --paper`` command runs, but with
:class:`KrakenBroker` as the market-data feed and per-symbol strategies from ``CRYPTO_SLEEVE``.
Every fill routes through ``PaperBroker(feed=KrakenBroker(...))`` — Kraken is used purely for live
quotes and candles, orders are simulated locally against those quotes, and nothing touches a real
Kraken account. Public data works without API credentials, so a bare ``.env`` is fine to run this.

Journals share the state directory with the equity paper session — ``paper_fills.jsonl``,
``paper_orders.jsonl``, ``paper_equity.csv`` — but each row is stamped with a distinct
``session_id`` (from PaperBroker), so the two runs stay separable in the record.

Reminder about the sleeve. ``CRYPTO_SLEEVE`` is tier=``screened`` (in-sample only), not
walk-forward-validated. Paper-only is the correct posture until the deep-history WF gate clears —
that is next-session item 2.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from trading_live_claude.analysis.universe import CRYPTO_SLEEVE
from trading_live_claude.brokers.kraken import KrakenBroker
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.config import get_settings
from trading_live_claude.data.cache import CandleCache
from trading_live_claude.data.market import MarketData
from trading_live_claude.execution.router import Router
from trading_live_claude.monitor.live_loop import LiveMonitor, MonitorEvent
from trading_live_claude.risk.sizing import PositionSizer
from trading_live_claude.strategies import STRATEGIES


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=300,
                    help="Poll interval, seconds. Kraken is 24/7 so this is real all the time.")
    ap.add_argument("--paper-equity", type=float, default=100_000.0)
    ap.add_argument("--iterations", type=int, default=0,
                    help="0 = run forever; a positive N runs that many polls and stops.")
    args = ap.parse_args()

    settings = get_settings()

    # KrakenBroker with no API creds is fine for paper: public data is unauth. If keys are present
    # in the environment we pass them through (Balance/positions become populated), but live-order
    # placement is still gated OFF here — the PaperBroker wraps this instance for every fill.
    feed = KrakenBroker(
        api_key=settings.kraken_api_key or "",
        api_secret=settings.kraken_api_secret or "",
        enable_live_orders=False,
    )
    exec_broker = PaperBroker(feed=feed, starting_equity=args.paper_equity,
                              journal_dir=Path(settings.state_dir))
    exec_account = exec_broker.accounts()[0].number
    print(f"[kraken-paper] PAPER mode. session_id={exec_broker.session_id} "
          f"starting_equity=${args.paper_equity:,.0f} account={exec_account}", flush=True)
    print("[kraken-paper] Kraken feed is quote/candle ONLY. Real Kraken account untouched.",
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

    # Build the per-symbol strategy map from CRYPTO_SLEEVE. The MAIN strategy is a fallback for any
    # symbol not in the map; here every routed symbol IS in the map, so the fallback should never
    # actually be selected — just picked as the sleeve's leader for clarity.
    smap = {entry.symbol: STRATEGIES[entry.strategy](**dict(entry.params))
            for entry in CRYPTO_SLEEVE.values()}
    fallback_entry = next(iter(CRYPTO_SLEEVE.values()))
    fallback = STRATEGIES[fallback_entry.strategy](**dict(fallback_entry.params))
    sym_list = list(CRYPTO_SLEEVE)
    print(f"[kraken-paper] monitoring {len(sym_list)} pairs: {sym_list}", flush=True)

    def _emit(ev: MonitorEvent) -> None:
        state = "NEW" if ev.is_transition else f"persisting ({ev.poll_count})"
        print(f"[kraken-paper] {ev.kind.upper()} {ev.symbol} @ {ev.price:.4f} "
              f"({state}) detail={ev.detail}", flush=True)

    monitor = LiveMonitor(
        broker=exec_broker,
        market=market,
        strategy=fallback,
        sizer=sizer,
        router=router,
        account_number=exec_account,
        symbols=sym_list,
        interval_seconds=args.interval,
        on_event=_emit,
        account_currency="USD",             # Kraken quotes are USD; the fiat side is ZUSD
        emit_on_change_only=False,          # persistence mode; edges + poll counts both preserved
        strategy_map=smap,
    )
    monitor.run_forever(max_iterations=args.iterations or None)


if __name__ == "__main__":
    main()
