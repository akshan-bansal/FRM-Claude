---
description: Place ONE order Claude has already decided on. No analysis; just execution.
allowed-tools: Bash
argument-hint: <buy|sell> <symbol> <shares> <stop> [target] [reason]
---

Parse `$ARGUMENTS` as: side symbol shares stop [target] [reason].

If `reason` missing, refuse — every order must have a logged rationale.

Run:
```
uv run trading place-order \
  --symbol <SYMBOL> --side <SIDE> --shares <SHARES> \
  --stop <STOP> --target <TARGET_OR_0> \
  --reason "<REASON>" --strategy llm-claude
```

Report the result: order_id on success, rejection reason on failure (read `state/rejected.jsonl` tail).

Refuse if `state/HALTED` exists (`test -f state/HALTED`).
