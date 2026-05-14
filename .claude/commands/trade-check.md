---
description: Full LLM-driven trading cycle — gather state, analyze, decide, optionally place an order.
allowed-tools: Bash, Read, Grep, Glob, WebFetch, WebSearch
argument-hint: [--symbols A,B,C] [--paper-only]
---

You are the trader. Algorithms are your helpers. Decide if and how to trade THIS minute.

## Phase 1 — gather state (parallel where possible)

Run all in parallel:
1. `uv run trading status` — equity, primary account
2. `uv run trading positions` — current holdings + open PnL
3. `uv run trading risk-report` — heat %, kill-switch state
4. `uv run trading autonomous status` — daily caps + state
5. `uv run python -c "from trading_live_claude.config import get_settings;s=get_settings();print(s.autonomous_symbols, s.account_currency, s.execution_mode, s.questrade_env)"` — config snapshot

Read the latest 20 lines of `state/orders.jsonl` if it exists (Bash: `tail -n 20 state/orders.jsonl`).

## Phase 2 — pull signals from algorithms

For each symbol in `autonomous_symbols` (or `--symbols` override):
- `uv run trading signal --strategy <STRATEGY> --symbols <SYMBOL> --iterations 1`

Capture: last close, indicator values, whether `entry` or `exit` fired on the last bar.

## Phase 3 — analyze (this is YOUR job, not the algorithm's)

Weigh:
- **Signal**: did the strategy fire entry/exit?
- **Regime**: is the broad market up/down/flat? Check with a quick web search if useful.
- **Risk state**: heat %, daily-loss progress, kill-switch.
- **Position sizing reality**: at current equity, can a trade clear `min_ticket_usd`? At ~$100 equity, only ZAG.TO and XIC.TO fit; VOO/IWM/AAPL/MSFT cost more than the whole account.
- **Commission drag**: $4.95 min sell commission. If account is small and edge per trade is small, hold over trade.
- **Existing positions**: do you have anything open already that conflicts?

Write a one-paragraph rationale BEFORE deciding.

## Phase 4 — decide and act

Outcomes:

### A. Buy
```
uv run trading place-order \
  --symbol <SYMBOL> --side buy --shares <N> \
  --stop <PRICE> --target <PRICE> \
  --reason "<your rationale verbatim>" \
  --strategy llm-claude
```

Size shares so that `(entry - stop) * shares <= equity * 0.02` (2% account risk). For Bollinger entry: stop = lower band - 0.5 * ATR.

### B. Sell (close existing)
```
uv run trading place-order \
  --symbol <SYMBOL> --side sell --shares <FULL_QTY> \
  --stop <PRICE> --reason "<rationale>" \
  --strategy llm-claude
```

### C. Hold
Just print your rationale. No CLI call. This is the default.

### D. Halt
If something looks wrong (data stale, position mismatched, broker error):
```
uv run trading kill --reason "<what's wrong>"
```

## Phase 5 — log + report

Print a final summary:
- What you observed
- What you decided
- What you did (order_id if placed, or "hold" or "halt")
- What you'll watch for next tick

## Hard rules

- **Never override the Router.** If `place-order` returns REJECTED, that's final. Don't retry with looser params.
- **Never edit `config/trading.yaml` to bypass a gate.** If a gate keeps firing, surface that to the user — let them adjust.
- **Default mode is `auto`** (uses execution_mode from yaml). If user passes `--paper-only`, add `--mode paper` to every place-order call.
- **Refuse to place orders if `state/HALTED` exists.** Run `test -f state/HALTED` first; if found, stop immediately.
- **Never run more than one BUY per symbol per session.** If you already placed one in this `/trade-check` invocation, don't place another.
