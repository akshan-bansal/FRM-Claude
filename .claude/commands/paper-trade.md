---
description: Start a paper-trading session against Questrade live quotes.
allowed-tools: Bash, Read
argument-hint: <strategy> <symbol[,symbol,...]> [iterations]
---

Spin up a paper-trading session.

Steps:
1. Parse `$ARGUMENTS`. Default iterations = 30 (so the session is bounded).
2. Run `uv run trading paper --strategy <STRATEGY> --symbols <SYMBOLS> --iterations <N>`.
3. While running, periodically read `state/paper_fills.jsonl` to surface any fills that occurred this session.
4. On finish, print a one-paragraph session summary: # entries, # exits, net cash change.

Refuse to escalate to `trading live`. If the user asks "now run it live", explicitly remind them they must:
1. Set `EXECUTION_MODE=live` in `.env`
2. Run `uv run trading live --confirm "I UNDERSTAND THE RISK" ...` themselves; you will not run it for them.
