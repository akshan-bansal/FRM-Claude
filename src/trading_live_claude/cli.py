"""Typer-based CLI: `trading <subcommand>`.

Every command keeps live-mode behind explicit flags. The default mode
across the board is ``paper``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .backtest import BacktestEngine
from .brokers import PaperBroker, QuestradeBroker, TokenStore
from .config import get_settings
from .daemon import AutonomousDaemon
from .data import CandleCache, MarketData
from .execution import AUTONOMOUS_ENV_VAR, AutonomousNotEnabled, LiveModeNotConfirmed, Router
from .execution.daily_budget import DailyBudget
from .logging_setup import configure_logging, get_logger
from .monitor import Alerter, LiveMonitor
from .monitor.alerter import AlertConfig
from .risk import KillSwitch, PositionSizer
from .strategies import STRATEGIES
from .tune import DEFAULT_TUNE_STRATEGIES, DEFAULT_TUNE_UNIVERSE, apply_tune, run_tune

app = typer.Typer(help="Claude Code algorithmic trading CLI (paper-first; live behind explicit flag).")
console = Console()
log = get_logger(__name__)


# ----- helpers ---------------------------------------------------------------


def _make_questrade(settings) -> QuestradeBroker:
    if not settings.questrade_refresh_token:
        console.print("[red]QUESTRADE_REFRESH_TOKEN missing. See README §Quick start.[/red]")
        raise typer.Exit(code=2)
    if not settings.token_encryption_key:
        console.print("[red]TOKEN_ENCRYPTION_KEY missing. Generate one and put it in .env.[/red]")
        raise typer.Exit(code=2)
    return QuestradeBroker.from_settings(
        refresh_token=settings.questrade_refresh_token,
        encryption_key=settings.token_encryption_key,
        state_dir=settings.state_dir,
    )


def _strategy_or_die(name: str):
    if name not in STRATEGIES:
        console.print(f"[red]Unknown strategy '{name}'. Available: {sorted(STRATEGIES)}[/red]")
        raise typer.Exit(code=2)
    return STRATEGIES[name]()


# ----- commands --------------------------------------------------------------


@app.callback()
def main_callback() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir)


@app.command()
def status() -> None:
    """Read-only sanity check: token exchange, account, balances."""
    settings = get_settings()
    broker = _make_questrade(settings)
    accounts = broker.accounts()
    table = Table(title="Questrade accounts")
    for col in ("number", "type", "status", "primary"):
        table.add_column(col)
    for a in accounts:
        table.add_row(a.number, a.type, a.status, str(a.isPrimary))
    console.print(table)
    if accounts:
        cur = settings.account_currency
        eq = broker.equity(accounts[0].number, currency=cur)
        console.print(f"Equity (primary, {cur}): [green]${eq:,.2f}[/green]")


@app.command()
def backtest(
    strategy: str = typer.Option(..., help="Strategy key (e.g. ema_crossover, rsi_meanrevert, macd, bollinger, momentum_breakout)"),
    symbol: str = typer.Option(..., help="Ticker, e.g. AAPL or SHOP.TO"),
    years: float = typer.Option(3.0, help="Years of history to fetch"),
    interval: str = typer.Option("1d", help="Candle interval: 1d, 1h, 30m, 15m, 5m, 1m"),
    out: Path = typer.Option(Path("reports"), help="Output dir for report"),
) -> None:
    """Run a single-symbol backtest and save a markdown report."""
    settings = get_settings()
    broker = _make_questrade(settings)
    cache = CandleCache(settings.data_cache_dir)
    market = MarketData(broker, cache=cache)
    df = market.history(symbol=symbol, years=years, interval=interval)
    if df.empty:
        console.print(f"[red]No data returned for {symbol}.[/red]")
        raise typer.Exit(code=1)

    strat = _strategy_or_die(strategy)
    engine = BacktestEngine()
    result = engine.run(strategy=strat, df=df, symbol=symbol, timeframe=interval)
    md = result.summary_markdown()
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / f"{strategy}_{symbol.replace('.', '_')}.md"
    report_path.write_text(md, encoding="utf-8")
    console.print(md)
    console.print(f"\n[green]Report written:[/green] {report_path}")


@app.command()
def signal(
    strategy: str = typer.Option(..., help="Strategy key"),
    symbols: str = typer.Option(..., help="Comma-separated symbols"),
    interval: int = typer.Option(60, help="Poll interval (seconds)"),
    iterations: int = typer.Option(0, help="Run N iterations and stop. 0 = forever."),
) -> None:
    """Live-signal monitor. Never places orders (dry-run router).

    Article skill #5 ("live-signal-monitor"): output signal only.
    """
    settings = get_settings()
    broker = _make_questrade(settings)
    market = MarketData(broker, cache=CandleCache(settings.data_cache_dir))
    strat = _strategy_or_die(strategy)
    sizer = PositionSizer(risk_pct=settings.risk_pct_per_trade)

    accounts = broker.accounts()
    if not accounts:
        console.print("[red]No accounts on Questrade login.[/red]")
        raise typer.Exit(code=1)
    account_number = settings.questrade_account_number or accounts[0].number

    router = Router.build_default(
        mode="dry-run",
        broker=broker,
        state_dir=settings.state_dir,
        cap_pct=settings.portfolio_heat_cap,
        max_drawdown_pct=settings.max_drawdown_kill_switch,
        daily_loss_limit_pct=settings.daily_loss_limit_pct,
        max_open_positions=settings.max_open_positions,
        min_ticket_usd=settings.min_ticket_usd,
    )

    alerter = Alerter(
        AlertConfig(
            telegram_bot_token=settings.telegram_bot_token,
            telegram_chat_id=settings.telegram_chat_id,
            smtp_host=settings.smtp_host,
            smtp_user=settings.smtp_user,
            smtp_pass=settings.smtp_pass,
            email_to=settings.alert_email_to,
        )
    )

    def _emit(ev) -> None:
        if ev.kind in {"entry", "exit"}:
            alerter.send(
                f"{strategy} {ev.kind.upper()}: {ev.symbol}",
                f"price={ev.price:.4f} detail={ev.detail}",
            )

    monitor = LiveMonitor(
        broker=broker,
        market=market,
        strategy=strat,
        sizer=sizer,
        router=router,
        account_number=account_number,
        symbols=[s.strip().upper() for s in symbols.split(",") if s.strip()],
        interval_seconds=interval,
        on_event=_emit,
        account_currency=settings.account_currency,
    )
    monitor.run_forever(max_iterations=iterations or None)


@app.command()
def paper(
    strategy: str = typer.Option(..., help="Strategy key"),
    symbols: str = typer.Option(..., help="Comma-separated symbols"),
    interval: int = typer.Option(60, help="Poll interval (seconds)"),
    iterations: int = typer.Option(0, help="Run N iterations and stop. 0 = forever."),
    starting_equity: float = typer.Option(100_000.0),
) -> None:
    """Paper-trade against an in-memory broker fed by Questrade quotes."""
    settings = get_settings()
    feed = _make_questrade(settings)
    pb = PaperBroker(feed=feed, starting_equity=starting_equity, journal_dir=settings.state_dir)
    market = MarketData(feed, cache=CandleCache(settings.data_cache_dir))
    strat = _strategy_or_die(strategy)
    sizer = PositionSizer(risk_pct=settings.risk_pct_per_trade)
    router = Router.build_default(
        mode="paper",
        broker=pb,
        state_dir=settings.state_dir,
        cap_pct=settings.portfolio_heat_cap,
        max_drawdown_pct=settings.max_drawdown_kill_switch,
        daily_loss_limit_pct=settings.daily_loss_limit_pct,
        max_open_positions=settings.max_open_positions,
        min_ticket_usd=settings.min_ticket_usd,
    )
    monitor = LiveMonitor(
        broker=pb,
        market=market,
        strategy=strat,
        sizer=sizer,
        router=router,
        account_number="PAPER-001",
        symbols=[s.strip().upper() for s in symbols.split(",") if s.strip()],
        interval_seconds=interval,
        account_currency=settings.account_currency,
    )
    monitor.run_forever(max_iterations=iterations or None)


@app.command()
def live(
    strategy: str = typer.Option(..., help="Strategy key"),
    symbols: str = typer.Option(..., help="Comma-separated symbols"),
    interval: int = typer.Option(60, help="Poll interval (seconds)"),
    confirm: str = typer.Option(..., help='Confirmation phrase. Must equal: I UNDERSTAND THE RISK'),
    iterations: int = typer.Option(0, help="Run N iterations and stop. 0 = forever."),
) -> None:
    """LIVE trading against your real Questrade account. Reads `state/HALTED` for kill-switch."""
    settings = get_settings()
    if settings.execution_mode != "live":
        console.print('[red]Set EXECUTION_MODE=live in .env before using `trading live`.[/red]')
        raise typer.Exit(code=2)
    if settings.questrade_env != "live":
        console.print('[yellow]QUESTRADE_ENV is not "live"; routing through practice account.[/yellow]')

    broker = _make_questrade(settings)
    accounts = broker.accounts()
    if not accounts:
        console.print("[red]No accounts.[/red]")
        raise typer.Exit(code=1)
    account_number = settings.questrade_account_number or accounts[0].number

    try:
        router = Router.build_default(
            mode="live",
            broker=broker,
            state_dir=settings.state_dir,
            cap_pct=settings.portfolio_heat_cap,
            max_drawdown_pct=settings.max_drawdown_kill_switch,
            daily_loss_limit_pct=settings.daily_loss_limit_pct,
            max_open_positions=settings.max_open_positions,
            min_ticket_usd=settings.min_ticket_usd,
            live_confirmation=confirm,
        )
    except LiveModeNotConfirmed as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from e

    market = MarketData(broker, cache=CandleCache(settings.data_cache_dir))
    strat = _strategy_or_die(strategy)
    sizer = PositionSizer(risk_pct=settings.risk_pct_per_trade)

    monitor = LiveMonitor(
        broker=broker,
        market=market,
        strategy=strat,
        sizer=sizer,
        router=router,
        account_number=account_number,
        symbols=[s.strip().upper() for s in symbols.split(",") if s.strip()],
        interval_seconds=interval,
        account_currency=settings.account_currency,
    )
    monitor.run_forever(max_iterations=iterations or None)


@app.command()
def kill(reason: str = typer.Option("manual", help="Reason recorded in state/HALTED")) -> None:
    """Trip the kill-switch immediately. Live router refuses orders while tripped."""
    settings = get_settings()
    KillSwitch(settings.state_dir).trip(reason=reason)
    console.print(f"[red]Kill-switch tripped: {reason}[/red]")


@app.command("clear-kill")
def clear_kill(
    ack: str = typer.Option(..., help='Must equal: I HAVE INVESTIGATED'),
) -> None:
    """Clear the kill-switch. Requires explicit ack phrase."""
    settings = get_settings()
    try:
        KillSwitch(settings.state_dir).clear(ack)
        console.print("[yellow]Kill-switch cleared.[/yellow]")
    except PermissionError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from e


@app.command()
def positions() -> None:
    """Show current Questrade positions for the primary account."""
    settings = get_settings()
    broker = _make_questrade(settings)
    accounts = broker.accounts()
    if not accounts:
        console.print("[red]No accounts.[/red]")
        return
    acc = settings.questrade_account_number or accounts[0].number
    pos = broker.positions(acc)
    if not pos:
        console.print("[dim]No open positions.[/dim]")
        return
    t = Table(title=f"Positions for {acc}")
    for col in ("symbol", "qty", "avg cost", "current px", "open PnL"):
        t.add_column(col)
    for p in pos:
        t.add_row(
            p.symbol,
            f"{p.openQuantity:g}",
            f"{p.averageEntryPrice:.2f}",
            f"{p.currentPrice:.2f}",
            f"{p.openPnl:+.2f}",
        )
    console.print(t)


@app.command("risk-report")
def risk_report() -> None:
    """Print current portfolio heat + VaR + kill-switch state."""
    settings = get_settings()
    broker = _make_questrade(settings)
    accounts = broker.accounts()
    if not accounts:
        return
    acc = settings.questrade_account_number or accounts[0].number
    eq = broker.equity(acc, currency=settings.account_currency)
    pos = broker.positions(acc)
    notional = sum(abs(p.openQuantity) * p.currentPrice for p in pos)
    heat_pct = notional / eq if eq else 0.0
    ks = KillSwitch(settings.state_dir).state()
    t = Table(title="Risk report")
    for col in ("metric", "value"):
        t.add_column(col)
    t.add_row("equity", f"${eq:,.2f}")
    t.add_row("open notional", f"${notional:,.2f}")
    t.add_row("portfolio heat", f"{heat_pct:.2%}  (cap {settings.portfolio_heat_cap:.0%})")
    t.add_row("kill switch", "HALTED" if ks.halted else "ok")
    if ks.halted:
        t.add_row("reason", ks.reason)
    console.print(t)


# ----- tune ------------------------------------------------------------------


@app.command()
def tune(
    years: float = typer.Option(5.0, help="Years of history per backtest"),
    symbols: str = typer.Option("", help="Comma-separated symbols (empty = curated default universe)"),
    strategies: str = typer.Option("", help="Comma-separated strategy keys (empty = all)"),
    dry_run: bool = typer.Option(False, help="Score + print, but don't write trading.yaml"),
    parallel: int = typer.Option(4, help="Concurrent backtests"),
) -> None:
    """Backtest a strategy x symbol grid; write the winning config to config/trading.yaml."""
    settings = get_settings()
    broker = _make_questrade(settings)
    cache = CandleCache(settings.data_cache_dir)

    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()] or list(DEFAULT_TUNE_UNIVERSE)
    strat_list = [s.strip() for s in strategies.split(",") if s.strip()] or list(DEFAULT_TUNE_STRATEGIES)

    console.print(
        f"[bold]Tuning[/bold] {len(strat_list)} strategies x {len(sym_list)} symbols, "
        f"years={years}, parallel={parallel}"
    )
    results = run_tune(broker, cache, symbols=sym_list, strategies=strat_list, years=years, parallel=parallel)
    if not results:
        console.print("[red]No backtests succeeded.[/red]")
        raise typer.Exit(code=1)

    table = Table(title="Scoreboard (top 15)")
    for col in ("strategy", "symbol", "Sharpe", "MaxDD", "CAGR", "Win%", "trades", "score"):
        table.add_column(col)
    for r in results[:15]:
        table.add_row(
            r.strategy,
            r.symbol,
            f"{r.sharpe:.2f}",
            f"{r.max_drawdown:.2%}",
            f"{r.cagr:.2%}",
            f"{r.win_rate:.2%}",
            str(r.num_trades),
            f"{r.score:.2f}",
        )
    console.print(table)

    update = apply_tune(results, dry_run=dry_run)
    if update is None:
        console.print(
            "[yellow]No combo passed filters (>= 15 trades, max DD >= -20%). trading.yaml unchanged.[/yellow]"
        )
        raise typer.Exit(code=1)
    if dry_run:
        console.print("[yellow]--dry-run: trading.yaml NOT written.[/yellow]")
    else:
        console.print(
            f"[green]trading.yaml updated:[/green] strategy={update['default_strategy']}  "
            f"symbols={update['default_symbols']}"
        )


# ----- autonomous subcommands ------------------------------------------------

autonomous_app = typer.Typer(help="Claude-driven autonomous trading loop. Background daemon process.")
app.add_typer(autonomous_app, name="autonomous")


def _build_alerter(settings) -> Alerter:
    return Alerter(
        AlertConfig(
            telegram_bot_token=settings.telegram_bot_token,
            telegram_chat_id=settings.telegram_chat_id,
            smtp_host=settings.smtp_host,
            smtp_user=settings.smtp_user,
            smtp_pass=settings.smtp_pass,
            email_to=settings.alert_email_to,
        )
    )


@autonomous_app.command("status")
def autonomous_status() -> None:
    """Show daemon state, today's trade count, today's notional."""
    settings = get_settings()
    daemon = AutonomousDaemon(settings.state_dir, settings.log_dir)
    st = daemon.status()
    budget = DailyBudget(
        settings.state_dir,
        max_trades_per_day=settings.autonomous_daily_max_trades,
        max_notional_per_day_usd=settings.autonomous_daily_max_notional_usd,
    ).snapshot()
    ks = KillSwitch(settings.state_dir).state()
    t = Table(title="Autonomous daemon")
    for col in ("metric", "value"):
        t.add_column(col)
    t.add_row("running", "yes" if st.running else "no")
    t.add_row("pid", str(st.pid) if st.pid else "-")
    t.add_row("pidfile", str(st.pidfile))
    t.add_row("stale pidfile", "yes" if st.stale else "no")
    t.add_row("kill-switch", "HALTED" if ks.halted else "ok")
    if ks.halted:
        t.add_row("kill reason", ks.reason)
    t.add_row("trades today", f"{budget.trades_today}/{budget.max_trades}")
    t.add_row("notional today", f"${budget.notional_today_usd:,.0f}/${budget.max_notional_usd:,.0f}")
    t.add_row("autonomous_enabled (.env)", str(settings.autonomous_enabled))
    t.add_row("autonomous_account (.env)", settings.autonomous_account)
    t.add_row("interval (s)", str(settings.autonomous_interval_seconds))
    t.add_row("strategy", settings.autonomous_strategy)
    t.add_row("symbols", settings.autonomous_symbols)
    console.print(t)


@autonomous_app.command("start")
def autonomous_start(
    account: str = typer.Option("", help="Override autonomous_account: practice|live"),
) -> None:
    """Spawn the autonomous daemon. Background process; survives this shell."""
    settings = get_settings()
    if not settings.autonomous_enabled:
        console.print(
            "[red]Refusing to start: AUTONOMOUS_ENABLED is false in .env. "
            "Read RISK_DISCLOSURE.md, then set AUTONOMOUS_ENABLED=true manually.[/red]"
        )
        raise typer.Exit(code=2)
    if KillSwitch(settings.state_dir).state().halted:
        console.print("[red]Refusing to start: kill-switch tripped. Clear it manually first.[/red]")
        raise typer.Exit(code=2)

    chosen_account = (account or settings.autonomous_account).lower()
    if chosen_account not in {"practice", "live"}:
        console.print(f"[red]Invalid account '{chosen_account}'. Use practice or live.[/red]")
        raise typer.Exit(code=2)

    extra_env = {
        AUTONOMOUS_ENV_VAR: "true",
        "AUTONOMOUS_ACCOUNT_RUNTIME": chosen_account,
    }
    daemon = AutonomousDaemon(settings.state_dir, settings.log_dir)
    argv = [sys.executable, "-m", "trading_live_claude.cli", "autonomous", "run"]
    try:
        pid = daemon.start(argv, extra_env=extra_env)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from e
    console.print(f"[green]Autonomous daemon started (pid={pid}, account={chosen_account}).[/green]")
    console.print(f"  stdout log: {daemon.stdout_log}")
    console.print(f"  stderr log: {daemon.stderr_log}")


@autonomous_app.command("stop")
def autonomous_stop() -> None:
    """Stop the autonomous daemon (graceful, with hard-kill fallback)."""
    settings = get_settings()
    daemon = AutonomousDaemon(settings.state_dir, settings.log_dir)
    if daemon.stop():
        console.print("[yellow]Autonomous daemon stopped.[/yellow]")
    else:
        console.print("[dim]No autonomous daemon running.[/dim]")


@autonomous_app.command("run")
def autonomous_run() -> None:
    """Foreground autonomous loop. Normally invoked by `autonomous start`, not by humans."""
    settings = get_settings()
    if not settings.autonomous_enabled:
        console.print("[red]AUTONOMOUS_ENABLED is false. Aborting.[/red]")
        raise typer.Exit(code=2)
    if KillSwitch(settings.state_dir).state().halted:
        console.print("[red]Kill-switch tripped. Aborting.[/red]")
        raise typer.Exit(code=2)

    chosen_account = os.environ.get("AUTONOMOUS_ACCOUNT_RUNTIME", settings.autonomous_account).lower()
    if chosen_account not in {"practice", "live"}:
        console.print(f"[red]Invalid AUTONOMOUS_ACCOUNT_RUNTIME='{chosen_account}'.[/red]")
        raise typer.Exit(code=2)

    # If the user picked live but Questrade env is practice (or vice-versa), reconcile
    # by spawning the broker against the matching endpoint. We honor `chosen_account`
    # over `.env` QUESTRADE_ENV for the lifetime of this process.
    os.environ["QUESTRADE_ENV"] = chosen_account

    broker = _make_questrade(settings)
    accounts = broker.accounts()
    if not accounts:
        console.print("[red]No accounts on Questrade login.[/red]")
        raise typer.Exit(code=1)
    account_number = settings.questrade_account_number or accounts[0].number

    market = MarketData(broker, cache=CandleCache(settings.data_cache_dir))
    strat = _strategy_or_die(settings.autonomous_strategy)
    sizer = PositionSizer(risk_pct=settings.risk_pct_per_trade)

    try:
        router = Router.build_default(
            mode="autonomous",
            broker=broker,
            state_dir=settings.state_dir,
            cap_pct=settings.portfolio_heat_cap,
            max_drawdown_pct=settings.max_drawdown_kill_switch,
            daily_loss_limit_pct=settings.daily_loss_limit_pct,
            max_open_positions=settings.max_open_positions,
            min_ticket_usd=settings.min_ticket_usd,
            daily_max_trades=settings.autonomous_daily_max_trades,
            daily_max_notional_usd=settings.autonomous_daily_max_notional_usd,
        )
    except (LiveModeNotConfirmed, AutonomousNotEnabled) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2) from e

    alerter = _build_alerter(settings)

    def _emit(ev) -> None:
        if ev.kind in {"entry", "exit"}:
            alerter.send(
                f"[autonomous/{chosen_account}] {settings.autonomous_strategy} {ev.kind.upper()}: {ev.symbol}",
                f"price={ev.price:.4f} detail={ev.detail}",
            )

    monitor = LiveMonitor(
        broker=broker,
        market=market,
        strategy=strat,
        sizer=sizer,
        router=router,
        account_number=account_number,
        symbols=settings.autonomous_symbols_list,
        interval_seconds=settings.autonomous_interval_seconds,
        on_event=_emit,
        account_currency=settings.account_currency,
    )
    log.info(
        "autonomous.loop.start",
        account=chosen_account,
        strategy=settings.autonomous_strategy,
        symbols=settings.autonomous_symbols_list,
        interval=settings.autonomous_interval_seconds,
    )
    monitor.run_forever()


@autonomous_app.command("tail")
def autonomous_tail(lines: int = typer.Option(30, help="Lines from each log")) -> None:
    """Print last N lines of stdout/stderr logs from the daemon."""
    settings = get_settings()
    daemon = AutonomousDaemon(settings.state_dir, settings.log_dir)
    for label, p in (("stdout", daemon.stdout_log), ("stderr", daemon.stderr_log)):
        console.print(f"[bold]{label} ({p}):[/bold]")
        if not p.exists():
            console.print("[dim](empty)[/dim]")
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in content[-lines:]:
                console.print(f"  {line}")
        except OSError as e:
            console.print(f"[red]Could not read: {e}[/red]")


# ----- place-order: the LLM-trader's hands ------------------------------------
#
# This is what the Claude Code session calls when it has done its analysis and
# wants to place a single order. The Router enforces every risk gate before the
# broker sees the order; Claude cannot bypass them regardless of what it
# "decides". On rejection the CLI exits non-zero so Claude can read the failure.


@app.command("place-order")
def place_order(
    symbol: str = typer.Option(..., help="Ticker, e.g. XIC.TO or AAPL"),
    side: str = typer.Option(..., help="buy | sell"),
    shares: int = typer.Option(..., help="Whole-share quantity (broker will reject fractions)"),
    stop: float = typer.Option(..., help="Protective stop price (used by Router sanity gate)"),
    target: float = typer.Option(0.0, help="Optional take-profit reference (informational)"),
    reason: str = typer.Option(..., help='Free-text rationale; logged to state/orders.jsonl'),
    strategy: str = typer.Option("llm-claude", help="Strategy/source tag for the journal"),
    mode: str = typer.Option(
        "auto",
        help='Router mode override: auto|paper|dry-run|live|autonomous. "auto" reads execution_mode.',
    ),
    json_output: bool = typer.Option(False, help="Emit machine-readable JSON instead of text"),
) -> None:
    """Place a single order. Claude calls this after analysis. All risk gates apply."""
    import json as _json
    from .brokers.models import OrderAction
    from .execution.router import OrderIntent

    settings = get_settings()
    chosen_mode = (mode if mode != "auto" else settings.execution_mode).lower()
    if chosen_mode not in {"paper", "dry-run", "live", "autonomous"}:
        console.print(f"[red]Invalid mode {chosen_mode!r}.[/red]")
        raise typer.Exit(code=2)
    if chosen_mode == "live":
        console.print(
            "[red]place-order refuses mode=live (typed confirmation phrase required).[/red] "
            "Use mode=autonomous (with AUTONOMOUS_ENABLED=true) or mode=paper instead."
        )
        raise typer.Exit(code=2)

    side_lower = side.lower().strip()
    if side_lower not in {"buy", "sell"}:
        console.print(f"[red]side must be buy or sell; got {side!r}[/red]")
        raise typer.Exit(code=2)
    action = OrderAction.BUY if side_lower == "buy" else OrderAction.SELL

    feed = _make_questrade(settings)
    broker = feed
    if chosen_mode == "paper":
        broker = PaperBroker(feed=feed, starting_equity=100_000.0, journal_dir=settings.state_dir)

    accounts = broker.accounts()
    if not accounts:
        console.print("[red]No account.[/red]")
        raise typer.Exit(code=1)
    account_number = settings.questrade_account_number or accounts[0].number
    equity = broker.equity(account_number, currency=settings.account_currency)

    # Approximate existing open-risk dollars (2% of notional as a stand-in stop).
    existing_positions = broker.positions(account_number)
    existing_risk = sum(abs(p.openQuantity) * p.currentPrice * 0.02 for p in existing_positions)
    open_positions_count = sum(1 for p in existing_positions if p.openQuantity != 0)

    risk_dollars = abs(shares * (stop - 0.0)) if action == OrderAction.SELL else abs(shares * (0.0 - stop))
    # Better: risk = shares * |entry - stop|. We don't have entry; use latest quote.
    try:
        quote_now = broker.quote(symbol)
    except Exception as e:
        console.print(f"[red]Quote fetch failed for {symbol}: {e}[/red]")
        raise typer.Exit(code=1) from e
    entry_ref = quote_now.mid or quote_now.lastTradePrice or 0.0
    if entry_ref <= 0:
        console.print(f"[red]No usable price for {symbol}.[/red]")
        raise typer.Exit(code=1)
    risk_dollars = abs(shares * (entry_ref - stop))

    intent = OrderIntent(
        symbol=symbol,
        action=action,
        shares=int(shares),
        entry=float(entry_ref),
        stop=float(stop),
        target=float(target) if target > 0 else None,
        strategy=strategy,
        risk_dollars=float(risk_dollars),
        account_number=account_number,
    )

    router = Router.build_default(
        mode="paper" if chosen_mode == "paper" else ("dry-run" if chosen_mode == "dry-run" else "autonomous"),
        broker=broker,
        state_dir=settings.state_dir,
        cap_pct=settings.portfolio_heat_cap,
        max_drawdown_pct=settings.max_drawdown_kill_switch,
        daily_loss_limit_pct=settings.daily_loss_limit_pct,
        max_open_positions=settings.max_open_positions,
        min_ticket_usd=settings.min_ticket_usd,
        daily_max_trades=settings.autonomous_daily_max_trades,
        daily_max_notional_usd=settings.autonomous_daily_max_notional_usd,
    ) if chosen_mode == "autonomous" else Router.build_default(
        mode="paper" if chosen_mode == "paper" else "dry-run",
        broker=broker,
        state_dir=settings.state_dir,
        cap_pct=settings.portfolio_heat_cap,
        max_drawdown_pct=settings.max_drawdown_kill_switch,
        daily_loss_limit_pct=settings.daily_loss_limit_pct,
        max_open_positions=settings.max_open_positions,
        min_ticket_usd=settings.min_ticket_usd,
    )

    placed = router.submit(
        intent,
        equity=equity,
        existing_risk=existing_risk,
        open_positions=open_positions_count,
    )
    payload = {
        "submitted": placed is not None,
        "order_id": placed.id if placed else None,
        "symbol": symbol,
        "side": side_lower,
        "shares": int(shares),
        "entry_ref": entry_ref,
        "stop": stop,
        "risk_dollars": risk_dollars,
        "mode": chosen_mode,
        "reason": reason,
    }
    if json_output:
        print(_json.dumps(payload))
    else:
        if placed:
            console.print(f"[green]PLACED[/green] {symbol} {side_lower} {shares} (order_id={placed.id})")
        else:
            console.print(f"[red]REJECTED[/red] {symbol} — see state/rejected.jsonl")
        console.print(payload)
    raise typer.Exit(code=0 if placed else 3)


def _entry() -> None:
    app()


if __name__ == "__main__":
    _entry()
