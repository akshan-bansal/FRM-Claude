# trading-live-claude

Claude Code–driven algorithmic trading framework for **Questrade** (Canadian + US equities / ETFs). Paper-trading by default. Live trading behind explicit flags + risk gates.

Built around the five Claude Code skills from [Top 5 Claude Code Skills for Algorithmic Trading](https://medium.datadriveninvestor.com/top-5-claude-code-skills-for-algorithmic-trading-49620fa2b02c) — rewired to use Questrade as the single data + execution source instead of EODHD.

---

## ⚠️ Risk disclosure

**This software can place real orders against your real Questrade account.** Algorithmic trading carries substantial financial risk. Bugs, network failures, stale tokens, and bad strategies can lose money fast. By using this repo you accept that:

- You are solely responsible for every order placed.
- Paper-mode is the default. `--live` mode requires a typed confirmation phrase + explicit env vars.
- Read [`RISK_DISCLOSURE.md`](RISK_DISCLOSURE.md) before flipping the live switch.
- Test exhaustively in `practice` (Questrade's free practice account) before touching real money.

---

## Quick start

```bash
# 1. Install (Python 3.13+ via uv)
uv sync

# 2. Bootstrap config
cp .env.example .env
# Open https://login.questrade.com/APIAccess/UserApps.aspx and create an app.
# Generate a refresh token, paste into .env (QUESTRADE_REFRESH_TOKEN=...).
# Generate a random TOKEN_ENCRYPTION_KEY (e.g. `python -c "import secrets;print(secrets.token_urlsafe(32))"`).

# 3. Validate connection (read-only)
uv run trading status

# 4. Run a backtest
uv run trading backtest --strategy ema_crossover --symbol SHOP.TO --years 3

# 5. Live-signal monitor (no orders placed)
uv run trading signal --strategy rsi_meanrevert --symbols AAPL,MSFT --interval 60

# 6. Paper trade (default mode)
uv run trading paper --strategy ema_crossover --symbols AAPL,XIC.TO

# 7. Live trade (requires explicit confirmation)
EXECUTION_MODE=live uv run trading live --strategy ema_crossover --symbols AAPL --confirm "I UNDERSTAND THE RISK"
```

---

## What ships in the box

### Code (`src/trading_live_claude/`)

| Layer | Module | Purpose |
|---|---|---|
| Broker | `brokers/questrade.py` | OAuth refresh, accounts, positions, market data, order placement |
| Data | `data/market.py`, `data/cache.py` | Historical candles + live quotes (Questrade), parquet cache |
| Risk | `risk/sizing.py`, `risk/var.py`, `risk/kill_switch.py` | ATR sizing, fixed-fractional, portfolio heat, Historical VaR, kill-switch |
| Signals | `signals/indicators.py`, `signals/generator.py` | EMA/SMA/RSI/MACD/ATR/Bollinger, no-lookahead enforcement |
| Strategies | `strategies/examples/*` | 6 working strategies (see below) |
| Backtest | `backtest/engine.py`, `backtest/metrics.py` | Vectorized engine, Sharpe / max DD / win-rate / equity curve |
| Execution | `execution/router.py`, `execution/paper.py`, `execution/live.py` | Paper sim + live router with risk gates |
| Monitor | `monitor/live_loop.py`, `monitor/alerter.py` | Real-time loop, Telegram/email alerts |
| CLI | `cli.py` | `trading` command (Typer) |

### Strategies (`strategies/examples/`)

- `ema_crossover.py` — Fast/slow EMA cross
- `rsi_meanrevert.py` — RSI(14) < 30 long, > 70 short
- `macd.py` — MACD signal-line cross
- `bollinger.py` — Mean-revert at 2σ bands
- `momentum_breakout.py` — N-day Donchian breakout
- `pairs.py` — Cointegrated pair z-score reversion

### Claude skills (`.claude/skills/`)

Six skills auto-load when this repo is the working directory:

1. **backtest-expert** — turns a plain-English strategy spec into a vectorized backtest (article skill #1, rewired for Questrade)
2. **market-data-pipeline** — standardized OHLCV fetch via Questrade (article skill #2)
3. **signal-generation** — strategy rules → executable signals, lookahead-bias check (article skill #3)
4. **risk-management** — ATR sizing, VaR, portfolio heat, kill-switch (article skill #4)
5. **live-signal-monitor** — polling loop + alerts, no execution (article skill #5)
6. **questrade-execution** — own skill: signal → risk gate → Questrade order, paper/live aware

### Claude slash commands (`.claude/commands/`)

- `/backtest` — run a backtest interactively
- `/signal-check` — quick live-signal snapshot
- `/paper-trade` — start a paper session
- `/live-trade-confirm` — multi-step live-trade approval ritual
- `/risk-report` — current account exposure, heat, VaR
- `/positions` — show current Questrade positions
- `/kill` — hit the kill-switch immediately

### Claude agents (`.claude/agents/`)

- `risk-gate` — read-only audit of any proposed order set before live submission
- `strategy-reviewer` — checks strategy code for lookahead, survivorship, overfitting smells

---

## Architecture

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for full diagrams. Two-line summary:

```
[Strategy] → [Signal] → [Risk Gate] → [Router (paper|live)] → [Broker (Questrade)]
                                  ↑
                          [Kill-switch / daily loss / heat checks]
```

The same `Strategy` + `Signal` pipeline runs in three modes:
- **Backtest** — historical OHLCV from Questrade, executes against simulated book
- **Paper** — live quotes from Questrade, executes against in-memory simulated book
- **Live** — live quotes from Questrade, executes against your real Questrade account

---

## Tokens & security

- The refresh token is one-shot: every successful refresh returns a **new** refresh token. The broker layer atomically rewrites `state/tokens.json` (encrypted with `TOKEN_ENCRYPTION_KEY`) on each refresh.
- Lose `state/tokens.json` → re-generate a refresh token at the Questrade app portal and update `.env`.
- `.env` is gitignored. Never commit it.

---

## Disclaimer

Not affiliated with Questrade or Anthropic. Educational use. No warranty. Past backtest performance does not predict future results. Read the source before running anything live.
