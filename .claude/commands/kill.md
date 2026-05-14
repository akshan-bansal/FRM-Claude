---
description: Trip the kill-switch immediately. Writes state/HALTED.
allowed-tools: Bash
argument-hint: <reason>
---

Run `uv run trading kill --reason "$ARGUMENTS"`. Confirm the sentinel file exists. Do NOT suggest the clear command — leaving it tripped is intentional.
