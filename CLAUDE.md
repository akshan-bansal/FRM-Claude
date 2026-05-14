# Project: trading-live-claude

Claude Code project memory. Loaded automatically when working in this repo.

## What this repo is

Algorithmic trading framework. Strategies → signals → risk gates → Questrade orders. Paper-mode default. Live mode behind multi-step confirmation.

## Non-negotiables

- **Never bypass the risk gate.** All order intents go through `execution.router.Router`. Even tests must use the paper router, never instantiate a Questrade broker for tests.
- **Never set `EXECUTION_MODE=live` in code, tests, or example snippets.** Live mode is a runtime decision by the human only.
- **Indicators are shifted before comparison.** Adding a strategy? Use `signals.generator.no_lookahead(df)` and write a regression test that fails on lookahead.
- **`state/` files are ground truth.** Don't compute equity, positions, or peak-from-scratch — read from journal.
- **Refresh tokens are one-shot.** The Questrade broker rewrites `state/tokens.json` atomically on every refresh. Don't add a second code path that reads/writes that file.

## Default style

- Python 3.13, `uv` for env, `ruff` for lint, `mypy --strict` for typing
- `httpx` + `hishel` for HTTP (NOT `requests` — global rule)
- `pydantic` v2 models at boundaries (broker responses, config)
- `structlog` for logging, no `print` outside CLI surface
- `pytest` with `respx` for HTTP mocking, no live calls in CI

## When the user asks for a new strategy

1. Subclass `strategies.base.Strategy`
2. Implement `generate_signals(self, df: pd.DataFrame) -> pd.DataFrame` returning columns `entry`, `exit`, `size_hint`
3. Add a backtest run command in the README example block
4. Write at least one test: a synthetic-data round-trip plus a lookahead-bias guard
5. Register in `strategies/__init__.py:STRATEGIES`

## When the user asks to backtest

Default flow: `uv run trading backtest --strategy NAME --symbol TICKER --years N`. Output is a markdown summary + equity-curve PNG written to `reports/`. Always flag overfitting risk if `years < 2` (per article skill #1).

## When the user asks to "go live"

Three checks before you run anything:
1. Confirm `QUESTRADE_ENV=practice` for at least one full week of paper trading first
2. Confirm `state/HALTED` does not exist
3. Confirm the user has typed the live-mode confirmation phrase

If any check fails, stop and report — do not edit the .env to make it pass.

## Useful entry points

- CLI: `src/trading_live_claude/cli.py`
- Router (the bottleneck): `src/trading_live_claude/execution/router.py`
- Risk gates: `src/trading_live_claude/risk/`
- Questrade client: `src/trading_live_claude/brokers/questrade.py`

## Skills available in this repo

`.claude/skills/`:
- `backtest-expert` — strategy spec → vectorized backtest
- `market-data-pipeline` — OHLCV / quotes via Questrade
- `signal-generation` — rules → signals (with lookahead check)
- `risk-management` — sizing, VaR, heat, kill switch
- `live-signal-monitor` — polling loop, alerts only, never orders
- `questrade-execution` — signal → risk gate → live order (own skill)
