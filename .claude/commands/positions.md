---
description: List current Questrade positions for the primary account.
allowed-tools: Bash
---

Run `uv run trading positions`.
If no positions: say so in one line.
Otherwise: render the table and call out any position whose open PnL is < -5%.
