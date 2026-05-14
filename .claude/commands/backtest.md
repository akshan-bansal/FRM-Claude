---
description: Run a backtest for a strategy on a single symbol using the local engine.
allowed-tools: Bash, Read
argument-hint: <strategy> <symbol> [years]
---

You are running a backtest in the local trading-live-claude repo.

Steps:
1. Parse args from `$ARGUMENTS`. Default years = 3 if not supplied.
2. Validate strategy is in `src/trading_live_claude/strategies/__init__.py:STRATEGIES`.
3. Run: `uv run trading backtest --strategy <STRATEGY> --symbol <SYMBOL> --years <YEARS>`
4. Read the markdown report from `reports/` and summarize the result in 6 bullet lines.
5. If the report contains a "Warnings" section, surface every warning verbatim.
6. Suggest one specific improvement (e.g., try a different param, add a filter, lengthen the window) based on the metrics.

Refuse to run if:
- The strategy name is unknown.
- The user passes `--live` or anything that would imply live execution. This command is read-only.
