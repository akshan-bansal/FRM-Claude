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

import asyncio

from trading_live_claude.analysis.universe import CRYPTO_SLEEVE
from trading_live_claude.brokers.kraken import KrakenBroker
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.config import get_settings
from trading_live_claude.data.cache import CandleCache
from trading_live_claude.data.kraken_ohlc import kraken_ohlc
from trading_live_claude.data.market import MarketData
from trading_live_claude.execution.router import Router
from trading_live_claude.intel.interpret import interpret
from trading_live_claude.intel.overlay import IntelSnapshot
from trading_live_claude.intel.routing import OverlayProvider
from trading_live_claude.intel.worldmonitor import WorldMonitorClient
from trading_live_claude.monitor.live_loop import LiveMonitor, MonitorEvent
from trading_live_claude.portfolio.allocator import PortfolioAllocator
from trading_live_claude.risk.sizing import PositionSizer
from trading_live_claude.strategies import STRATEGIES


def _compute_allocator_bias(pairs: dict) -> dict[str, float]:
    """Run the correlation-aware allocator over the sleeve and return per-symbol conviction bias.

    Bias = allocator_weight / equal_weight_baseline. A pair the allocator concentrates on gets
    bias > 1.0 (boost), a redundant pair in a correlated cluster gets < 1.0 (trim). Equal-weight
    is the neutral case at exactly 1.0.

    Fetches shallow Kraken daily OHLC (~720 bars/pair) once at startup — the cadence for a
    correlation matrix refresh is weekly at most, so a start-of-session compute is fine. If any
    pair's fetch fails, its bias is 1.0 (neutral) rather than dropping it from the sleeve.
    """
    import pandas as pd
    returns: dict[str, pd.Series] = {}
    scores: dict[str, float] = {}
    for routed, entry in pairs.items():
        try:
            df = kraken_ohlc(entry.pair, interval=1440)
            returns[routed] = df.set_index("time")["close"].pct_change().dropna()
            scores[routed] = float(entry.screen_score)
        except Exception as e:
            print(f"[allocator] {routed}: fetch failed ({e}); bias defaults to 1.0", flush=True)
    if not returns:
        return {r: 1.0 for r in pairs}
    # Cap per-name at 0.30 so no single pair dominates; sleeve-level is one sleeve so it doesn't
    # matter. min_score=0 so every positive-scoring pair gets a slot.
    allocator = PortfolioAllocator(max_weight=0.30, max_sleeve_weight=1.0, min_score=0.0)
    result = allocator.allocate(returns, scores, regime_scalar=1.0)
    if not result.weights:
        return {r: 1.0 for r in pairs}
    equal_weight = 1.0 / len(returns)
    bias = {r: (result.weights.get(r, 0.0) / equal_weight) if equal_weight > 0 else 1.0
            for r in pairs}
    return bias


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

    # Intel overlay + interpret bias — the same wires the equity `signal --intel-overlay` uses.
    # OverlayProvider computes a per-class de-risk scalar (crypto is the one that matters here)
    # AND writes to state/intel_graph.jsonl on every refresh, so this loop's polling now feeds
    # the graph journal too (closing the gap where Kraken paper contributed nothing to intel).
    # Interpret_for reuses provider.last_snapshot so the reasoning layer sees the exact same
    # snapshot the overlay decided on — no drift.
    overlay_for = None
    interpret_for = None
    if settings.worldmonitor_api_key:
        def _snapshot() -> IntelSnapshot:
            async def _f() -> IntelSnapshot:
                async with WorldMonitorClient(settings.worldmonitor_api_key) as wm:
                    return await wm.snapshot()
            return asyncio.run(_f())
        overlay_provider = OverlayProvider(_snapshot, refresh_seconds=900.0)
        overlay_for = overlay_provider

        def _interpret_current():
            snap = overlay_provider.last_snapshot
            return interpret(snap) if snap is not None else []
        interpret_for = _interpret_current
        print("[kraken-paper] intel overlay ON — crypto class scalar + interpret filter live; "
              "polls contribute to state/intel_graph.jsonl", flush=True)
    else:
        print("[kraken-paper] intel overlay OFF (no WORLDMONITOR_API_KEY) — sleeve runs on "
              "allocator + strategy signal only", flush=True)

    # Correlation-aware allocator: compute per-pair conviction bias from daily OHLC + screen
    # score. Runs once at startup; the correlation matrix is stable enough at daily cadence
    # that a start-of-session compute is fine (weekly refresh cadence at most).
    print("[kraken-paper] computing correlation-aware allocator weights...", flush=True)
    bias_map = _compute_allocator_bias(CRYPTO_SLEEVE)
    print("[kraken-paper] allocator conviction bias (baseline = 1.0):", flush=True)
    for sym in sorted(bias_map, key=lambda s: -bias_map[s]):
        arrow = "boost" if bias_map[sym] > 1.05 else "trim" if bias_map[sym] < 0.95 else "neutral"
        print(f"    {sym:>10}  x{bias_map[sym]:.2f}  ({arrow})", flush=True)

    def _weight_bias_for(symbol: str) -> float:
        return bias_map.get(symbol, 1.0)

    # Alerter — silent to phone without this. Mirrors the QT CLI wiring. Credentials from settings;
    # empty creds means stdout-only, so the venue works whether or not .env has keys.
    from trading_live_claude.monitor import Alerter
    from trading_live_claude.monitor.alerter import AlertConfig
    from trading_live_claude.intel.notification import (
        format_entry as _fmt_entry,
        format_exit as _fmt_exit,
    )
    alerter = Alerter(AlertConfig(
        telegram_bot_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
        smtp_host=settings.smtp_host,
        smtp_user=settings.smtp_user,
        smtp_pass=settings.smtp_pass,
        email_to=settings.alert_email_to,
    ))
    strategy_name_for = {entry.symbol: entry.strategy for entry in CRYPTO_SLEEVE.values()}

    def _emit(ev: MonitorEvent) -> None:
        state = "NEW" if ev.is_transition else f"persisting ({ev.poll_count})"
        print(f"[kraken-paper] {ev.kind.upper()} {ev.symbol} @ {ev.price:.4f} "
              f"({state}) detail={ev.detail}", flush=True)
        if ev.kind == "entry":
            sname = strategy_name_for.get(ev.symbol, fallback_entry.strategy)
            title, body = _fmt_entry(strategy_name=sname, symbol=ev.symbol, price=ev.price,
                                       detail=ev.detail,
                                       is_transition=getattr(ev, "is_transition", True),
                                       poll_count=getattr(ev, "poll_count", 1))
            alerter.send(title, body)
        elif ev.kind == "exit":
            sname = strategy_name_for.get(ev.symbol, fallback_entry.strategy)
            shares = int(ev.detail.get("shares", 0)) if isinstance(ev.detail, dict) else 0
            title, body = _fmt_exit(strategy_name=sname, symbol=ev.symbol, price=ev.price,
                                      shares=shares)
            alerter.send(title, body)

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
        weight_bias_for=_weight_bias_for,
        overlay_for=overlay_for,
        interpret_for=interpret_for,
    )
    monitor.run_forever(max_iterations=args.iterations or None)


if __name__ == "__main__":
    main()
