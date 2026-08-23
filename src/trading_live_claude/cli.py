"""Typer-based CLI: `trading <subcommand>`.

Every command keeps live-mode behind explicit flags. The default mode
across the board is ``paper``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from .analysis import build_signal_matrix, render_matrix_markdown
from .backtest import BacktestEngine
from .brokers import PaperBroker, QuestradeBroker
from .config import get_settings
from .daemon import AutonomousDaemon
from .data import CandleCache, MarketData
from .execution import AUTONOMOUS_ENV_VAR, AutonomousNotEnabled, LiveModeNotConfirmed, Router
from .execution.asset_router import ASSET_CLASSES, AssetRouter
from .execution.daily_budget import DailyBudget
from .integrations import (
    QuantConnectClient,
    QuantConnectError,
    analyze_library,
    list_library,
    pull_algorithm,
)
from .integrations.lean_algorithm import (
    DEFAULT_LEAN_ALGORITHM,
    LEAN_CANDLESTICK_MAP,
    comprehensive_lean_algorithms,
    gap_family_algorithms,
    render_candlestick_lean_algorithm,
    render_lean_algorithm,
)
from .logging_setup import configure_logging, get_logger
from .monitor import Alerter, LiveMonitor
from .monitor.alerter import AlertConfig
from .risk import KillSwitch, PositionSizer
from .scoring.objective import DEFAULT_METRIC_WEIGHTS, OBJECTIVES, make_dot_product
from .scoring.qc_bridge import rank_qc_library
from .scoring.routing import (
    route_symbols_to_strategies,
    strategy_asset_plan,
    to_strategy_map_string,
)
from .scoring.selection import (
    combine_scores,
    family_coverage,
    rank_cells,
    render_combined_scoreboard,
    score_strategies,
)
from .strategies import STRATEGIES, Strategy
from .tune import DEFAULT_TUNE_STRATEGIES, DEFAULT_TUNE_UNIVERSE, apply_tune, run_tune

app = typer.Typer(help="Claude Code algorithmic trading CLI (paper-first; live behind explicit flag).")
console = Console()
log = get_logger(__name__)

_DEFAULT_REPORTS_DIR = Path("reports")


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


def _make_qc(settings) -> QuantConnectClient:
    if not settings.quantconnect_user_id or not settings.quantconnect_api_token:
        console.print(
            "[red]QUANTCONNECT_USER_ID / QUANTCONNECT_API_TOKEN missing in .env. "
            "Get them at https://www.quantconnect.com/account.[/red]"
        )
        raise typer.Exit(code=2)
    return QuantConnectClient(settings.quantconnect_user_id, settings.quantconnect_api_token)


def _run_qc_flow(
    client: QuantConnectClient, project: str, content: str, name: str, timeout: float
) -> tuple[int, str, dict[str, object]]:
    """Create project → upload → compile → backtest on QC cloud; return ids + result."""
    if not client.authenticate():
        console.print("[red]QuantConnect authentication failed.[/red]")
        raise typer.Exit(code=1)
    proj = client.create_project(project, language="Py")
    projects = proj.get("projects")
    project_id = 0
    if isinstance(projects, list) and projects and isinstance(projects[0], dict):
        project_id = int(str(projects[0].get("projectId", 0)))
    console.print(f"[dim]project {project_id} created[/dim]")

    client.put_file(project_id, "main.py", content)
    compile_id = str(client.compile_project(project_id).get("compileId", ""))
    console.print(f"[dim]compiling ({compile_id})…[/dim]")
    client.wait_for_compile(project_id, compile_id)

    bt = client.create_backtest(project_id, compile_id, name)
    backtest = bt.get("backtest", {})
    backtest_id = str(backtest.get("backtestId", "")) if isinstance(backtest, dict) else ""
    console.print(f"[dim]backtest {backtest_id} running…[/dim]")
    result = client.wait_for_backtest(project_id, backtest_id, timeout_seconds=timeout)
    return project_id, backtest_id, result


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
    out: Path = typer.Option(_DEFAULT_REPORTS_DIR, help="Output dir for report"),
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
    strategy: str = typer.Option(..., help="Default/fallback strategy key"),
    symbols: str = typer.Option(..., help="Comma-separated symbols"),
    interval: int = typer.Option(60, help="Poll interval (seconds)"),
    iterations: int = typer.Option(0, help="Run N iterations and stop. 0 = forever."),
    strategy_map: str = typer.Option(
        "", help="Per-symbol strategy overrides, e.g. 'XIC.TO=bollinger,BNS.TO=momentum_breakout'"
    ),
) -> None:
    """Live-signal monitor. Never places orders (dry-run router).

    Article skill #5 ("live-signal-monitor"): output signal only. Use --strategy-map
    to monitor each symbol with its own strategy (e.g. the tuner's best-per-symbol);
    symbols not in the map use --strategy as the fallback.
    """
    settings = get_settings()
    broker = _make_questrade(settings)
    market = MarketData(broker, cache=CandleCache(settings.data_cache_dir))
    strat = _strategy_or_die(strategy)
    sizer = PositionSizer(risk_pct=settings.risk_pct_per_trade)

    smap: dict[str, Strategy] = {}
    for pair in strategy_map.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            console.print(f"[red]Bad --strategy-map entry '{pair}'. Use SYMBOL=strategy.[/red]")
            raise typer.Exit(code=2)
        sym, sname = pair.split("=", 1)
        smap[sym.strip().upper()] = _strategy_or_die(sname.strip())

    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    for sym in smap:  # include map-only symbols in what we monitor
        if sym not in sym_list:
            sym_list.append(sym)

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
            sname = smap[ev.symbol].name if ev.symbol in smap else strat.name
            alerter.send(
                f"{sname} {ev.kind.upper()}: {ev.symbol}",
                f"price={ev.price:.4f} detail={ev.detail}",
            )

    monitor = LiveMonitor(
        broker=broker,
        market=market,
        strategy=strat,
        sizer=sizer,
        router=router,
        account_number=account_number,
        symbols=sym_list,
        interval_seconds=interval,
        on_event=_emit,
        account_currency=settings.account_currency,
        strategy_map=smap,
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

    table = Table(title="Scoreboard (top 15) — scored by Sortino / |maxDD|")
    for col in ("strategy", "symbol", "Sortino", "Sharpe", "MaxDD", "CAGR", "Win%", "trades", "score"):
        table.add_column(col)
    for r in results[:15]:
        table.add_row(
            r.strategy,
            r.symbol,
            f"{r.sortino:.2f}",
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


@app.command(name="qc-backtest")
def qc_backtest(
    project: str = typer.Option("frm-claude-backtest", help="QuantConnect project name to create"),
    algorithm: str = typer.Option("", help="Path to a LEAN algorithm .py (empty = bundled EMA-cross starter)"),
    name: str = typer.Option("frm-claude run", help="Backtest name"),
    timeout: float = typer.Option(900.0, help="Seconds to wait for the backtest to finish"),
) -> None:
    """Push a LEAN algorithm to QuantConnect, compile, backtest, and print stats.

    Runs on QuantConnect's cloud via the REST API (needs QUANTCONNECT_USER_ID /
    QUANTCONNECT_API_TOKEN in .env). This drives a LEAN algorithm — it does NOT
    translate this repo's pandas strategies. Pass --algorithm to use your own
    LEAN file; otherwise a bundled EMA-cross starter is used.
    """
    settings = get_settings()
    client = _make_qc(settings)

    if algorithm:
        path = Path(algorithm)
        if not path.is_file():
            console.print(f"[red]Algorithm file not found: {path}[/red]")
            raise typer.Exit(code=2)
        content = path.read_text(encoding="utf-8")
    else:
        content = DEFAULT_LEAN_ALGORITHM

    try:
        project_id, backtest_id, result = _run_qc_flow(client, project, content, name, timeout)
    except QuantConnectError as e:
        console.print(f"[red]QuantConnect error: {e}[/red]")
        raise typer.Exit(code=1) from e
    finally:
        client.close()

    final = result.get("backtest", {})
    stats = final.get("statistics", {}) if isinstance(final, dict) else {}
    table = Table(title=f"QuantConnect backtest — {name}")
    table.add_column("statistic")
    table.add_column("value", justify="right")
    if isinstance(stats, dict) and stats:
        for k, v in stats.items():
            table.add_row(str(k), str(v))
    else:
        table.add_row("(no statistics returned)", "-")
    console.print(table)
    console.print(f"[green]Done.[/green] project={project_id} backtest={backtest_id}")


@app.command(name="qc-library")
def qc_library(
    pull: int = typer.Option(0, help="Project id to pull LEAN source for (0 = just list)"),
    analyze: bool = typer.Option(False, help="Read source to detect indicators/asset-class/family"),
) -> None:
    """List the QuantConnect projects in your account (your cloned strategy library).

    Alpha Streams is retired and the global library has no search API, so this reads
    your own account's projects via the base API. --pull <id> prints LEAN source;
    --analyze reads every project's code and detects its signals + family.
    """
    settings = get_settings()
    client = _make_qc(settings)
    try:
        if pull:
            sources = pull_algorithm(client, pull)
            if not sources:
                console.print(f"[yellow]No files in project {pull}.[/yellow]")
                raise typer.Exit(code=1)
            for fname, src in sources.items():
                console.print(f"[bold cyan]--- {fname} ---[/bold cyan]")
                console.print(src)
            return

        if analyze:
            analyses = analyze_library(client)
            if not analyses:
                console.print("[yellow]No projects found.[/yellow]")
                raise typer.Exit(code=1)
            table = Table(title=f"QC library — signal analysis ({len(analyses)} projects)")
            for col in ("projectId", "name", "family", "indicators", "assets", "symbols"):
                table.add_column(col)
            for a in sorted(analyses, key=lambda x: x.family):
                table.add_row(
                    str(a.project_id), a.name[:32], a.family,
                    ", ".join(a.indicators) or "-",
                    ", ".join(a.asset_classes) or "-",
                    ", ".join(a.symbols[:4]) or "-",
                )
            console.print(table)
            return

        strategies = list_library(client)
    finally:
        client.close()

    if not strategies:
        console.print("[yellow]No projects found in your QuantConnect account.[/yellow]")
        raise typer.Exit(code=1)
    table = Table(title=f"QuantConnect library ({len(strategies)} projects)")
    for col in ("projectId", "name", "lang", "category"):
        table.add_column(col)
    for s in sorted(strategies, key=lambda x: x.category):
        table.add_row(str(s.project_id), s.name[:60], s.language, s.category)
    console.print(table)


@app.command(name="signal-matrix")
def signal_matrix(
    symbols: str = typer.Option("", help="Comma-separated symbols (empty = curated tune universe)"),
    strategies: str = typer.Option("", help="Comma-separated strategies (empty = all single-symbol)"),
    years: float = typer.Option(5.0, help="Years of history per symbol"),
    horizon: int = typer.Option(10, help="Label horizon in bars (the forward window)"),
    up_threshold: float = typer.Option(0.03, help="Forward-return threshold that defines a real move"),
    rank: str = typer.Option(
        "", help="Rank cells by an objective (e.g. dot_product, roc_auc, precision); empty = sensitivity x specificity"
    ),
    rank_weights: str = typer.Option(
        "", help="Custom dot_product axis weights, e.g. 'precision=0.4,fidelity=0.3,sensitivity=0.1,specificity=0.1,risk=0.1'"
    ),
) -> None:
    """Build the sensitivity x specificity x risk report across strategy x universe.

    Sensitivity (recall) = real +move events caught; specificity = non-moves avoided;
    risk = max drawdown. Writes reports/signal_matrix.md and prints the top cells.
    Use --rank <objective> to order by any scoring objective (5-D dot_product, roc_auc,
    …); --rank-weights overrides the dot_product axis weights for bespoke ranking.
    """
    if rank and rank not in OBJECTIVES:
        console.print(f"[red]Unknown --rank objective '{rank}'. Options: {sorted(OBJECTIVES)}[/red]")
        raise typer.Exit(code=2)

    weights: dict[str, float] | None = None
    if rank_weights:
        if rank and rank != "dot_product":
            console.print(f"[red]--rank-weights only applies to dot_product, not '{rank}'.[/red]")
            raise typer.Exit(code=2)
        known = set(DEFAULT_METRIC_WEIGHTS)
        weights = {}
        for pair in rank_weights.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                console.print(f"[red]Bad --rank-weights entry '{pair}'. Use axis=weight.[/red]")
                raise typer.Exit(code=2)
            axis, val = pair.split("=", 1)
            axis = axis.strip()
            if axis not in known:
                console.print(f"[red]Unknown axis '{axis}'. Known axes: {sorted(known)}[/red]")
                raise typer.Exit(code=2)
            weights[axis] = float(val)
        rank = "dot_product"  # weights imply the dot-product objective
    settings = get_settings()
    broker = _make_questrade(settings)
    market = MarketData(broker, cache=CandleCache(settings.data_cache_dir))

    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()] or list(DEFAULT_TUNE_UNIVERSE)
    strat_list = [s.strip() for s in strategies.split(",") if s.strip()] or None

    frames: dict[str, pd.DataFrame] = {}
    for sym in sym_list:
        try:
            frames[sym] = market.history(symbol=sym, years=years, interval="1d")
        except Exception as e:
            console.print(f"[yellow]skip {sym}: {e}[/yellow]")

    cells = build_signal_matrix(frames, strategies=strat_list, horizon=horizon, up_threshold=up_threshold)
    if not cells:
        console.print("[red]No cells produced (no symbol had enough history).[/red]")
        raise typer.Exit(code=1)

    out = _DEFAULT_REPORTS_DIR / "signal_matrix.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_matrix_markdown(cells), encoding="utf-8")

    if rank:
        objective_fn = make_dot_product(weights) if weights is not None else rank
        ranked = rank_cells(cells, objective=objective_fn)[:15]
        label = f"dot_product {weights}" if weights is not None else rank
        title = f"Signal matrix — ranked by {label} ({len(cells)} cells)"
        table = Table(title=title)
        for col in ("score", "strategy", "symbol", "sens", "spec", "prec", "auc", "fid", "maxDD"):
            table.add_column(col)
        for c, sc in ranked:
            table.add_row(
                f"{sc:.3f}", c.strategy, c.symbol, f"{c.recall:.1%}", f"{c.specificity:.1%}",
                f"{c.precision:.1%}", f"{c.roc_auc:.2f}", f"{c.fidelity:+.2f}", f"{c.max_drawdown:.1%}",
            )
    else:
        top = sorted(cells, key=lambda c: (c.recall * c.specificity, -abs(c.max_drawdown)), reverse=True)[:15]
        table = Table(title=f"Signal matrix — sensitivity x specificity x risk ({len(cells)} cells)")
        for col in ("strategy", "symbol", "sens", "spec", "prec", "auc", "fid", "maxDD"):
            table.add_column(col)
        for c in top:
            table.add_row(
                c.strategy, c.symbol, f"{c.recall:.1%}", f"{c.specificity:.1%}", f"{c.precision:.1%}",
                f"{c.roc_auc:.2f}", f"{c.fidelity:+.2f}", f"{c.max_drawdown:.1%}",
            )
    console.print(table)
    console.print(f"[green]Report written:[/green] {out}")


@app.command(name="route")
def route(
    symbols: str = typer.Option("", help="Comma-separated symbols (empty = curated tune universe)"),
    strategies: str = typer.Option("", help="Comma-separated strategies (empty = all single-symbol)"),
    objective: str = typer.Option("dot_product", help="Objective to route by (dot_product, roc_auc, precision, …)"),
    years: float = typer.Option(5.0, help="Years of history per symbol"),
    top_n: int = typer.Option(3, help="Assets to list per strategy"),
    min_score: float = typer.Option(0.0, help="Drop symbols whose best score is below this floor"),
) -> None:
    """Strategy-conditioned asset selection + per-symbol routing.

    Prints (1) each strategy's best assets and (2) each symbol routed to its single
    best strategy, plus a ready-to-paste --strategy-map string for `trading signal`.
    """
    if objective not in OBJECTIVES:
        console.print(f"[red]Unknown objective '{objective}'. Options: {sorted(OBJECTIVES)}[/red]")
        raise typer.Exit(code=2)

    settings = get_settings()
    broker = _make_questrade(settings)
    market = MarketData(broker, cache=CandleCache(settings.data_cache_dir))
    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()] or list(DEFAULT_TUNE_UNIVERSE)
    strat_list = [s.strip() for s in strategies.split(",") if s.strip()] or None

    frames: dict[str, pd.DataFrame] = {}
    for sym in sym_list:
        try:
            frames[sym] = market.history(symbol=sym, years=years, interval="1d")
        except Exception as e:
            console.print(f"[yellow]skip {sym}: {e}[/yellow]")

    cells = build_signal_matrix(frames, strategies=strat_list)
    if not cells:
        console.print("[red]No cells produced.[/red]")
        raise typer.Exit(code=1)

    floor = min_score if min_score != 0.0 else None
    plan = strategy_asset_plan(cells, objective=objective, top_n=top_n, min_score=floor)
    routing = route_symbols_to_strategies(cells, objective=objective, min_score=floor)

    sel = Table(title=f"Asset selection by strategy — {objective}")
    for col in ("strategy", "family", "best assets (score)"):
        sel.add_column(col)
    for strat, entries in plan.items():
        if not entries:
            continue
        assets = "  ".join(f"{e.symbol}({e.score:.2f})" for e in entries)
        sel.add_row(strat, entries[0].family, assets)
    console.print(sel)

    rte = Table(title=f"Routing — symbol → best strategy ({len(routing)} routed)")
    for col in ("symbol", "strategy", "family", "score"):
        rte.add_column(col)
    for e in sorted(routing.values(), key=lambda e: e.score, reverse=True):
        rte.add_row(e.symbol, e.strategy, e.family, f"{e.score:.3f}")
    console.print(rte)

    map_str = to_strategy_map_string(routing)
    console.print("\n[bold]Deploy to the monitor:[/bold]")
    console.print(f"[dim]trading signal --strategy rsi_meanrevert --symbols \"{','.join(routing)}\" --strategy-map \"{map_str}\"[/dim]")


@app.command(name="qc-seed")
def qc_seed(
    dry_run: bool = typer.Option(False, help="Show the plan without creating projects"),
    limit: int = typer.Option(0, help="Max projects to create (0 = all)"),
) -> None:
    """Seed the QC account with a comprehensive, tunable strategy set across all domains.

    Creates one detectable LEAN project per template spanning momentum, mean-reversion,
    volatility, seasonality, and every QC-mappable candlestick pattern. All knobs are
    exposed via self.GetParameter(...) so you can run QC parameter optimization + your
    own universe selection over the pool later.
    """
    from collections import Counter

    algos = list(comprehensive_lean_algorithms().items())
    if limit:
        algos = algos[:limit]
    by_family = Counter(fam for _, (fam, _) in algos)
    console.print(f"[bold]Seeding {len(algos)} tunable strategies[/bold] by family: {dict(by_family)}")

    settings = get_settings()
    client = _make_qc(settings)
    try:
        existing = {str(p.get("name", "")) for p in client.list_projects()}
        for proj_name, (fam, source) in algos:
            if proj_name in existing:
                console.print(f"[dim]{fam}: '{proj_name}' already exists — skip[/dim]")
                continue
            if dry_run:
                console.print(f"[dim]{fam}: would create '{proj_name}'[/dim]")
                continue
            resp = client.create_project(proj_name, language="Py")
            projects = resp.get("projects")
            pid = 0
            if isinstance(projects, list) and projects and isinstance(projects[0], dict):
                pid = int(str(projects[0].get("projectId", 0)))
            client.put_file(pid, "main.py", source)
            console.print(f"[green]{fam:14}[/green] {proj_name} → project {pid}")
    except QuantConnectError as e:
        console.print(f"[red]QuantConnect error: {e}[/red]")
        raise typer.Exit(code=1) from e
    finally:
        client.close()

    if not dry_run:
        console.print("[dim]Re-run `trading qc-ingest` to see the full pool + coverage.[/dim]")


@app.command(name="qc-fill-gaps")
def qc_fill_gaps(
    dry_run: bool = typer.Option(False, help="Show what would be created without creating"),
) -> None:
    """Create gap-filling strategies in your QC account, one per missing family.

    QC's base API has no clone-from-library call, so this generates a detectable LEAN
    strategy for each family with no QC coverage (mean-reversion / volatility /
    seasonality / candlestick) and creates it as a project. Re-run `qc-ingest` after.
    """
    settings = get_settings()
    client = _make_qc(settings)
    try:
        analyses = analyze_library(client)
        coverage = family_coverage(STRATEGIES.keys(), [a.family for a in analyses])
        gaps = {fc.family for fc in coverage if fc.gap and fc.family != "other"}
        templates = gap_family_algorithms()
        to_create = {fam: nv for fam, nv in templates.items() if fam in gaps}

        if not to_create:
            console.print("[green]No gap families with an available template — nothing to create.[/green]")
            return

        console.print(f"[bold]Filling {len(to_create)} gap families:[/bold] {sorted(to_create)}")
        for fam, (proj_name, source) in to_create.items():
            if dry_run:
                console.print(f"[dim]{fam}: would create '{proj_name}'[/dim]")
                continue
            resp = client.create_project(proj_name, language="Py")
            projects = resp.get("projects")
            pid = 0
            if isinstance(projects, list) and projects and isinstance(projects[0], dict):
                pid = int(str(projects[0].get("projectId", 0)))
            client.put_file(pid, "main.py", source)
            console.print(f"[green]{fam}[/green] → created '{proj_name}' (project {pid})")
    except QuantConnectError as e:
        console.print(f"[red]QuantConnect error: {e}[/red]")
        raise typer.Exit(code=1) from e
    finally:
        client.close()

    if not dry_run:
        console.print("[dim]Re-run `trading qc-ingest` to see the gaps filled.[/dim]")


@app.command(name="qc-ingest")
def qc_ingest(
    symbols: str = typer.Option("", help="Universe for the native pool (empty = curated tune universe)"),
    with_native: bool = typer.Option(True, help="Also build the native scoreboard and merge into one ranked pool"),
    objective: str = typer.Option("dot_product", help="Objective for native scoring / combined ranking"),
) -> None:
    """Ingest cloned QC strategies, categorize + rank them, and report diversity gaps.

    Reads every project in your QC account, detects its family from the code, ranks it
    by its QC backtest, and (with --with-native) merges it with the native 36 into one
    ranked pool. The family-coverage table flags which families have no QC coverage —
    the ones to clone more of before going live.
    """
    settings = get_settings()
    client = _make_qc(settings)
    try:
        analyses = analyze_library(client)
        qc_scores = rank_qc_library(client, objective="sharpe_over_dd")
    except QuantConnectError as e:
        console.print(f"[red]QuantConnect error: {e}[/red]")
        raise typer.Exit(code=1) from e
    finally:
        client.close()

    if not analyses:
        console.print("[yellow]No QC projects found to ingest.[/yellow]")
        raise typer.Exit(code=1)

    ingest = Table(title=f"Ingested QC strategies ({len(analyses)} projects)")
    for col in ("projectId", "name", "family", "indicators"):
        ingest.add_column(col)
    for a in sorted(analyses, key=lambda x: x.family):
        ingest.add_row(str(a.project_id), a.name[:30], a.family, ", ".join(a.indicators) or "-")
    console.print(ingest)

    # Family-coverage gap report: native vs ingested QC.
    coverage = family_coverage(STRATEGIES.keys(), [a.family for a in analyses])
    cov = Table(title="Family coverage — native vs QC (gap = no QC strategy in that family)")
    for col in ("family", "native", "qc", "gap"):
        cov.add_column(col)
    for fc in coverage:
        cov.add_row(fc.family, str(fc.native), str(fc.qc), "[red]GAP[/red]" if fc.gap else "ok")
    console.print(cov)
    gaps = [fc.family for fc in coverage if fc.gap and fc.family != "other"]
    if gaps:
        console.print(f"[yellow]Clone QC strategies for these thin families before live:[/yellow] {gaps}")

    if with_native:
        broker = _make_questrade(settings)
        market = MarketData(broker, cache=CandleCache(settings.data_cache_dir))
        sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()] or list(DEFAULT_TUNE_UNIVERSE)
        frames: dict[str, pd.DataFrame] = {}
        for sym in sym_list:
            try:
                frames[sym] = market.history(symbol=sym, years=5.0, interval="1d")
            except Exception as e:
                console.print(f"[yellow]skip {sym}: {e}[/yellow]")
        native = score_strategies(build_signal_matrix(frames), objective=objective)
        combined = combine_scores(native, qc_scores)
        out = _DEFAULT_REPORTS_DIR / "combined_pool.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_combined_scoreboard(combined), encoding="utf-8")
        pool = Table(title=f"Combined pool — native + QC (top 15 of {len(combined)})")
        for col in ("source", "name", "family", "objective", "value"):
            pool.add_column(col)
        for c in combined[:15]:
            pool.add_row(c.source, c.name[:24], c.family, c.objective, f"{c.objective_value:.3f}")
        console.print(pool)
        console.print(f"[green]Combined pool written:[/green] {out}")


@app.command(name="qc-rank")
def qc_rank(
    objective: str = typer.Option("sharpe_over_dd", help="Objective to rank by (scoring.objective registry)"),
) -> None:
    """Rank your QC-library strategies by their latest backtest, via the objective adapter.

    Reads each project's most recent completed backtest and scores it (Sharpe / drawdown).
    Projects without a saved backtest are skipped. Family is detected from source code so
    these rank in the same taxonomy as native strategies.
    """
    settings = get_settings()
    client = _make_qc(settings)
    try:
        scores = rank_qc_library(client, objective=objective)
    except QuantConnectError as e:
        console.print(f"[red]QuantConnect error: {e}[/red]")
        raise typer.Exit(code=1) from e
    finally:
        client.close()

    if not scores:
        console.print(
            "[yellow]No QC projects with completed backtests to rank.[/yellow] "
            "Run a backtest on a project in QuantConnect (or via qc-deploy), then retry."
        )
        raise typer.Exit(code=1)

    table = Table(title=f"QC library ranking — {objective} ({len(scores)} projects)")
    for col in ("rank", "projectId", "name", "family", "Sharpe", "DD", "objective"):
        table.add_column(col)
    for i, s in enumerate(scores, start=1):
        table.add_row(
            str(i), str(s.project_id), s.name[:32], s.family,
            f"{s.sharpe:.2f}", f"{s.drawdown:.2%}", f"{s.objective_value:.3f}",
        )
    console.print(table)


@app.command(name="qc-deploy")
def qc_deploy(
    asset_class: str = typer.Option("equity", help=f"Asset class: one of {list(ASSET_CLASSES)}"),
    symbols: str = typer.Option("SPY", help="Comma-separated symbols (first is used as primary)"),
    project: str = typer.Option("frm-claude-deploy", help="QuantConnect project name"),
    name: str = typer.Option("frm-claude deploy", help="Backtest name"),
    dry_run: bool = typer.Option(True, help="Dry-run = generate+compile+backtest only (no live)"),
    timeout: float = typer.Option(900.0, help="Seconds to wait for the backtest"),
    pattern: str = typer.Option("", help="Candlestick pattern to trade via QC CandlestickPatterns (e.g. hammer)"),
) -> None:
    """Route an asset class to its brokerage, generate a LEAN algo, and dry-run it.

    The asset-class→brokerage router picks the LEAN venue; the generator emits a
    matching subscription (AddEquity/AddFuture/AddCrypto). --dry-run (default) runs a
    cloud backtest only. LIVE deploy is intentionally gated (see below).
    """
    if asset_class not in ASSET_CLASSES:
        console.print(f"[red]Unknown asset class '{asset_class}'. Choose from {list(ASSET_CLASSES)}.[/red]")
        raise typer.Exit(code=2)

    router = AssetRouter()
    decision = router.route(asset_class)
    console.print(
        f"[bold]Routing[/bold] {asset_class} → brokerage [cyan]{decision.brokerage}[/cyan] "
        f"(LEAN {decision.add_method}{', ' + decision.market if decision.market else ''})"
    )

    if not dry_run:
        console.print(
            "[red]LIVE deploy is gated.[/red] Live LEAN execution needs a QuantConnect "
            "live node + a connected brokerage that supports this asset class "
            f"([cyan]{decision.brokerage}[/cyan]). Set that up in QuantConnect, then deploy "
            "from there. This command only performs cloud dry-run backtests."
        )
        raise typer.Exit(code=2)

    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not sym_list:
        console.print("[red]No symbols given.[/red]")
        raise typer.Exit(code=2)
    if pattern:
        if pattern not in LEAN_CANDLESTICK_MAP:
            console.print(
                f"[red]Pattern '{pattern}' has no LEAN equivalent. QC-deployable: "
                f"{sorted(LEAN_CANDLESTICK_MAP)}[/red]"
            )
            raise typer.Exit(code=2)
        console.print(f"[dim]using QC CandlestickPatterns.{LEAN_CANDLESTICK_MAP[pattern]} for '{pattern}'[/dim]")
        content = render_candlestick_lean_algorithm(
            pattern=pattern, symbol=sym_list[0], add_method=decision.add_method, market=decision.market
        )
    else:
        content = render_lean_algorithm(
            symbol=sym_list[0], add_method=decision.add_method, market=decision.market
        )

    settings = get_settings()
    client = _make_qc(settings)
    try:
        project_id, backtest_id, result = _run_qc_flow(client, project, content, name, timeout)
    except QuantConnectError as e:
        console.print(f"[red]QuantConnect error: {e}[/red]")
        raise typer.Exit(code=1) from e
    finally:
        client.close()

    final = result.get("backtest", {})
    stats = final.get("statistics", {}) if isinstance(final, dict) else {}
    table = Table(title=f"QC dry-run — {asset_class}:{sym_list[0]} via {decision.brokerage}")
    table.add_column("statistic")
    table.add_column("value", justify="right")
    if isinstance(stats, dict) and stats:
        for k, v in stats.items():
            table.add_row(str(k), str(v))
    else:
        table.add_row("(no statistics returned)", "-")
    console.print(table)
    console.print(f"[green]Dry-run done.[/green] project={project_id} backtest={backtest_id}")


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
