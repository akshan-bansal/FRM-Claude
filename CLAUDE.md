# Project: trading-live-claude

Claude Code project memory. Loaded automatically when working in this repo.

## What this repo is

Algorithmic trading framework. Strategies → signals → risk gates → Questrade orders. Paper-mode default. Live mode behind multi-step confirmation.

## Config layout

This repo splits configuration so you can manage it freely without ever touching secrets:

- **`.env`** — secrets only. Refresh token, encryption key, optional SMTP/Telegram creds. Gitignored. **Reading `.env` is denied** in `.claude/settings.json`. You cannot see it.
- **`config/trading.yaml`** — every non-secret knob (strategy, symbols, risk caps, autonomous config). Gitignored (per-machine). **You can read and write this freely.**
- **`config/trading.example.yaml`** — committed template; what `trading.yaml` should look like.

Precedence: `trading.yaml` > environment variables > `.env` > dataclass defaults.

Use `uv run trading tune` (or the `/tune` command) to backtest a strategy x symbol grid and atomically rewrite `trading.yaml` with the winning config.

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

## Two trading modes — pick the right one

This repo supports **two distinct ways to trade** that share the same Router and risk gates:

### A. LLM-driven trader (you are the trader)

- Triggered by `/trade-check` or `/trade-now`.
- The `claude-trader` skill describes your decision framework. Read it before deciding.
- You gather state via `trading status / positions / risk-report / signal`, weigh the algorithm's signal against macro/cost/fit, then call `trading place-order` (which still routes through Router).
- Use `/loop 10m /trade-check` to put yourself on a polling schedule. Stops when the user closes Claude.
- Default mode for orders is `--mode auto` which honors `execution_mode` in `trading.yaml`. Refuse `--mode live` unless the user has explicitly typed "live" in this session.

### B. Deterministic autonomous daemon (Python decides)

## Autonomous mode

This repo supports a fully-autonomous Claude-driven trading loop. **Read these rules before touching anything related to it.**

- The daemon process is owned by the user's system, not by Claude. Claude can start/stop/inspect it, but the loop runs in `trading_live_claude.cli:autonomous_run` as a detached process with its own pid.
- A daemon spawned in this repo can place real Questrade orders if `AUTONOMOUS_ACCOUNT=live`. Default and reset value is `practice`.
- Autonomous router mode requires the runtime env var `AUTONOMOUS_ENABLED=true` AND `autonomous_enabled=true` in `.env`. The CLI sets the env var when calling `autonomous start`; the daemon then re-checks on every Router construction.
- The daemon respects ALL the standard gates (kill-switch, heat, per-trade risk, max positions, min ticket) PLUS a daily trade-count and daily notional cap.

### What Claude is allowed to do
- Run `/autonomous status`, `/autonomous tail`, `/autonomous start`, `/autonomous stop`.
- Use the `autonomous-monitor` agent to audit health.
- Recommend a stop if it sees a pattern of rejections, broker errors, or unexpected fills.

### What Claude is NOT allowed to do
- Flip `AUTONOMOUS_ENABLED` in `.env`. The user does that themselves.
- Flip `AUTONOMOUS_ACCOUNT` from `practice` to `live` in `.env`. The user does that themselves.
- Clear the kill-switch under any circumstance.
- Edit the Router's gate list, the DailyBudget caps, or the strategy code while the daemon is running.

### Failure modes to watch for
1. Token expiry mid-session → broker raises `TokenExpired`. Daemon continues looping; next iteration re-refreshes. If repeated, stop daemon.
2. Symbol not found → broker raises `BrokerError`. Daemon logs and skips that symbol; healthy.
3. Daily-budget exhausted → router rejects with reason "daily trade cap reached" / "daily notional cap". Healthy; daemon should idle until midnight UTC.
4. Pidfile present but process dead → `status` reports `stale`. `start` clears stale pidfile and proceeds.

## TradeCard — physical Ed25519 approval card (opt-in)

An optional third gate sits between the Router's risk checks and broker dispatch: a
physical approval card the user taps ACCEPT / DECLINE on. It's disabled by default and
opt-in per-run via `--require-card` on `paper_ib.py` / `paper_kraken.py`.

- **Never weakens existing gates.** `ApprovalRouter` runs `Router._gate` FIRST, and only
  publishes a prompt to the card if the router would have accepted the intent anyway.
  A card ACCEPT is a *human* gate, on top of the automated ones, not instead of them.
- **Autonomous mode cannot be wrapped.** `AUTONOMOUS_ENABLED=true` + card approval are
  mutually exclusive; the wrapper raises at construction. Pick one loop.
- **WYSIWYS canonical bytes.** The card signs a `|`-joined string that includes the
  broker (`ib` / `kraken` / `questrade`), action, symbol, shares, entry, notional,
  account, intent id, and nonce. A MITM cannot silently swap the broker or resize the
  intent — the signature won't verify.
- **Thesis from the VS engine.** `intel/vs_engine.py` composes a `<=140` char thesis
  (overlay risk clauses have priority over user-supplied notes during truncation) and
  persists a full writeup to `state/intel_writeups/{intel_ref}.json`. Framing is
  research not advice — clauses describe observed signal state, never issue directives.
- **URL versioning.** Every non-public route lives under `/v1/…` (SPEC_VERSION 1.0.0).
  `/healthz` and `/openapi.json` stay unversioned so a fresh client can bootstrap.
  Legacy unversioned paths still work during the deprecation window; the shim logs a
  warning on every hit.

Layout:

- `src/trading_live_claude/execution/approval.py` — router wrapper, store, registry,
  `wire_card_approval()` helper.
- `src/trading_live_claude/execution/approval_server.py` — stdlib HTTP surface (kept out
  of `execution.approval` so the module dependency arrow points inward).
- `scripts/approval_shim.py` — thin CLI wrapper for running the shim as a standalone
  process rather than a daemon thread beside the paper loop.
- `scripts/approval_card_sim.py` — laptop-side fake card that speaks the wire protocol
  end-to-end (register / poll / sign / respond). Real firmware in `firmware/tradecard/`
  is a separate ESP-IDF project; the Python wheel does not ship it.

To exercise without silicon:
```
python scripts/approval_shim.py --port 8787
python scripts/approval_card_sim.py --auto accept
python -m trading_live_claude.cli signal --paper --require-card ...
```

Gap-closure objectives live in `NEXT_SESSION.md` section 11.

## Skills available in this repo

`.claude/skills/`:
- `backtest-expert` — strategy spec → vectorized backtest
- `market-data-pipeline` — OHLCV / quotes via Questrade
- `signal-generation` — rules → signals (with lookahead check)
- `risk-management` — sizing, VaR, heat, kill switch
- `live-signal-monitor` — polling loop, alerts only, never orders
- `questrade-execution` — signal → risk gate → live order (own skill)

`.claude/commands/`:
- `/autonomous status|start|stop|tail` — manage the autonomous daemon

`.claude/agents/`:
- `autonomous-monitor` — periodic health audit of the daemon
- `risk-gate` — pre-flight audit before going live (autonomous or human-live)
- `strategy-reviewer` — code review for lookahead/overfitting
