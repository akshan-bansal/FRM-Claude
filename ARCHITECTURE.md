# Architecture

## Layered design

```
┌─────────────────────────────────────────────────────────────┐
│  CLI (typer)  /  Claude slash commands  /  Claude skills    │
├─────────────────────────────────────────────────────────────┤
│  Orchestration: backtest engine  │  monitor loop            │
├─────────────────────────────────────────────────────────────┤
│  Strategies (base class + 6 examples)                       │
├─────────────────────────────────────────────────────────────┤
│  Signal generator   │   Indicators (vectorized, no-lookahead)│
├─────────────────────────────────────────────────────────────┤
│  Risk manager   │   Kill-switch   │   Portfolio state       │
├─────────────────────────────────────────────────────────────┤
│  Execution router  ──►  Paper book  /  Live order placement │
├─────────────────────────────────────────────────────────────┤
│  Broker abstraction  ──►  Questrade REST + OAuth refresh    │
├─────────────────────────────────────────────────────────────┤
│  Data layer  ──►  Questrade candles/quotes + parquet cache  │
└─────────────────────────────────────────────────────────────┘
```

## Data flow (live mode)

```
                   ┌──────────────────┐
        every N    │  Monitor loop    │
        seconds    └────────┬─────────┘
                            │
                            ▼
                   ┌──────────────────┐
                   │ Questrade quote  │ ◄── OAuth-refreshed broker
                   │ + recent candles │
                   └────────┬─────────┘
                            │
                            ▼
                   ┌──────────────────┐
                   │ Strategy.signal()│
                   └────────┬─────────┘
                            │ Signal: BUY/SELL/HOLD + size hint
                            ▼
                   ┌──────────────────┐
                   │  Risk Manager    │  ATR stop, fractional sizing
                   └────────┬─────────┘
                            │ Order intent: symbol, qty, stop, target
                            ▼
                   ┌──────────────────┐         ┌──────────────┐
                   │  Risk Gates      │── fail ►│ rejected.log │
                   │  (heat, daily DD,│         └──────────────┘
                   │   kill switch)   │
                   └────────┬─────────┘
                            │ pass
                            ▼
                   ┌──────────────────┐
                   │  Router          │  paper | dry-run | live
                   └────────┬─────────┘
                            │
              ┌─────────────┴────────────┐
              ▼                          ▼
   ┌──────────────────┐         ┌──────────────────┐
   │ Paper book       │         │ Questrade orders │
   │ (in-memory + JSON│         │ (REST POST)      │
   │  fills journal)  │         └──────────────────┘
   └──────────────────┘
```

## Key invariants

1. **Indicators are always shifted before signal comparison.** Lookahead-bias prevention is encoded in `signals.generator.no_lookahead()` and validated by a `strategy-reviewer` agent.
2. **The router is the only thing that places orders.** Strategies and the monitor loop never touch the broker directly.
3. **The risk gate is the only thing that can pass the router into live mode.** A live `Router(mode="live")` instance raises `LiveModeNotConfirmed` unless constructed via `Router.confirm_live()`.
4. **State is durable.** Positions, fills, equity history, and the rolling peak are all on disk under `state/`. Restart is idempotent.
5. **Kill-switch is a file.** `state/HALTED` blocks every live order. Easy to flip manually, impossible to forget once flipped.

## Mode matrix

| Mode | Data | Orders | Use case |
|---|---|---|---|
| `backtest` | Historical Questrade candles | Simulated against historical bars | Strategy development |
| `dry-run` | Live Questrade quotes | None (logged only) | Sanity-check production signals |
| `paper` | Live Questrade quotes | In-memory simulated book | End-to-end validation, default mode |
| `live` (`QUESTRADE_ENV=practice`) | Live practice quotes | Practice-account orders | Pre-production validation |
| `live` (`QUESTRADE_ENV=live`) | Live quotes | Real-money orders | Production |

## File layout

```
src/trading_live_claude/
  brokers/
    base.py              # Broker protocol
    questrade.py         # OAuth + REST client
    paper.py             # in-memory broker for paper mode
  data/
    market.py            # candle / quote fetchers
    cache.py             # parquet-backed cache
    symbols.py           # symbol resolution + Questrade symbolId lookup
  signals/
    indicators.py        # ema, sma, rsi, macd, atr, bollinger, donchian
    generator.py         # rules -> entry/exit Series, lookahead check
  strategies/
    base.py              # Strategy ABC
    examples/
      ema_crossover.py
      rsi_meanrevert.py
      macd.py
      bollinger.py
      momentum_breakout.py
      pairs.py
  risk/
    sizing.py            # ATR / fixed-fractional position sizing
    var.py               # Historical VaR
    heat.py              # portfolio heat tracker
    kill_switch.py
  backtest/
    engine.py            # vectorized engine
    metrics.py           # Sharpe, DD, win rate
  execution/
    router.py            # mode-aware dispatcher with risk gates
    paper.py             # paper book
    live.py              # questrade order placement
    journal.py           # orders/fills jsonl writer
  monitor/
    live_loop.py
    alerter.py
  config/
    settings.py          # pydantic Settings from .env
    symbols.yaml
  cli.py                 # typer entrypoint
  logging_setup.py       # structlog config
```
