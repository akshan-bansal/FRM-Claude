---
description: Auto-tune trading.yaml — backtests a strategy x symbol grid and writes the winning config.
allowed-tools: Bash, Read
argument-hint: [--years N] [--dry-run] [--symbols A,B,C] [--strategies x,y]
---

Auto-tune the trading config. Steps:

1. Run `uv run trading tune $ARGUMENTS` (forward whatever flags the user passed).
2. Render the scoreboard table as-is.
3. After update, print `config/trading.yaml` so the user sees what changed.
4. Suggest the user run `/autonomous status` to confirm the daemon will pick up the new config on next restart.

Refuse if the user passes anything that would route to `trading live` or change `EXECUTION_MODE=live`. Tune is read-only on the broker side (backtests only).

If `--dry-run` is passed, do NOT suggest restarting the daemon; just summarize the scoreboard.

After tuning, if the user wants the daemon to pick up the new config immediately, recommend `uv run trading autonomous stop` followed by `uv run trading autonomous start` — but only if the daemon was running before.
