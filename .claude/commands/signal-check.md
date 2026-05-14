---
description: One-shot live-signal snapshot. No orders placed.
allowed-tools: Bash, Read
argument-hint: <strategy> <symbol[,symbol,...]>
---

Take a single snapshot of live signals for the listed symbols. Never enters a polling loop.

Steps:
1. Parse `$ARGUMENTS` as `<strategy> <symbols>`.
2. Run `uv run trading signal --strategy <STRATEGY> --symbols <SYMBOLS> --iterations 1`.
3. Capture stdout/stderr.
4. Summarize: which symbols are entry, which are exit, which are hold.
5. If any symbol triggered entry: print the would-be sized order from the alerter output, and remind the user that no order was placed.

Refuse to run with `--iterations 0` or any flag that implies continuous run.
