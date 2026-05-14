---
description: Manage the autonomous trading daemon (start/stop/status/tail).
allowed-tools: Bash, Read
argument-hint: <status|start|stop|tail|run> [args]
---

You manage the autonomous daemon for the user.

Workflow by subcommand:

### `status` (default if no args)
Run `uv run trading autonomous status`. Render output as-is.

### `start [--account practice|live]`
1. First, run `uv run trading status` to confirm Questrade auth works.
2. Then `uv run trading autonomous status` — if daemon already running, abort.
3. Read `.env` indirectly: `uv run python -c "from trading_live_claude.config import get_settings;s=get_settings();print(s.autonomous_enabled, s.autonomous_account, s.autonomous_daily_max_trades, s.autonomous_daily_max_notional_usd)"`.
4. If `autonomous_enabled` is False, refuse and tell user to set `AUTONOMOUS_ENABLED=true` in `.env` themselves.
5. Run `uv run trading autonomous start` (pass `--account` if provided).
6. After start, immediately run `uv run trading autonomous tail --lines 20` to surface boot logs.

### `stop`
1. Run `uv run trading autonomous stop`.
2. Print confirmation. Suggest running `status` to verify.

### `tail`
Run `uv run trading autonomous tail --lines 50`. Pretty-print.

### `run`
**REFUSE.** This is the foreground loop spawned by `start`. Tell the user to use `start` instead.

## Hard rules
- Never run with `--account live` unless the user explicitly typed "live" in their request. Default to practice account.
- If the kill-switch is tripped (`state/HALTED` exists), refuse start and explain why.
- Never clear the kill-switch from this command.
