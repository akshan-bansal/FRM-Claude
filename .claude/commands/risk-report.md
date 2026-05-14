---
description: Show current equity, exposure, heat, VaR, kill-switch state.
allowed-tools: Bash, Read
---

Run `uv run trading risk-report` and re-render the output as a markdown table. If kill-switch is HALTED, surface the reason in bold and stop — do not suggest clearing it.
