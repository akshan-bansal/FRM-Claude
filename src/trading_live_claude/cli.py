"""Typer-based CLI: `trading <subcommand>`.

Every command keeps live-mode behind explicit flags. The default mode
across the board is ``paper``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .backtest import BacktestEngine
from .brokers import PaperBroker, QuestradeBroker, TokenStore
from .config import get_settings
from .data import CandleCache, MarketData
from .execution import LiveModeNotConfirmed, Router
from .logging_setup import configure_logging, get_logger
from .monitor import Alerter, LiveMonitor
from .monitor.alerter import AlertConfig
from .risk import KillSwitch, PositionSizer
from .strategies import STRATEGIES

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
        eq = broker.equity(accounts[0].number)
        console.print(f"Equity (primary): [green]${eq:,.2f}[/green]")


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
    eq = broker.equity(acc)
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


def _entry() -> None:
    app()


if __name__ == "__main__":
    _entry()
