---
name: questrade-execution
description: Use when the user wants to take a signal-driven order intent and submit it to Questrade (paper, dry-run, or live). This is the ONLY skill that authorizes order placement. Enforces all risk gates and the kill-switch; refuses live mode unless an explicit confirmation phrase is provided.
---

# questrade-execution

You are the execution gatekeeper. Every order proposed by a strategy or sized by the risk-management skill flows through `trading_live_claude.execution.router.Router`. Your job is to call the router correctly and explain what happened.

## Recipe

1. **Acquire equity, open positions, existing risk.** Read from the broker (`broker.equity`, `broker.positions`), not from cached state — the broker is the single source of truth.
2. **Build the `OrderIntent`.** Use the sizer's output to fill `shares`, `entry`, `stop`, `target`, `risk_dollars`. Set `strategy=<strategy.name>` so the journal records provenance.
3. **Choose the router mode.**
   - `paper` — `Router.build_default(mode="paper", broker=PaperBroker(...))`. Default.
   - `dry-run` — `Router.build_default(mode="dry-run", broker=QuestradeBroker(...))`. No orders placed; only logged.
   - `live` — `Router.build_default(mode="live", live_confirmation="I UNDERSTAND THE RISK", ...)`. Refuses without the exact phrase.
4. **Submit.** `router.submit(intent, equity=..., existing_risk=..., open_positions=...)`. The router runs every gate. A `None` return means the order was rejected; check `state/rejected.jsonl`.
5. **Log the outcome** to the user: accepted (+ order id), rejected (+ reasons), or held in dry-run (+ reasons it would have been placed).
6. **Stop-loss is a separate child order.** Questrade does not bundle entry+stop atomically; submit the entry market order first, await fill, then submit a separate stop order. The framework v1 sends the entry only — surfacing the protective stop is on the operator until v2.

## Live-mode preconditions

Refuse to construct `Router.confirm_live()` unless ALL of:

1. `state/HALTED` does not exist.
2. `EXECUTION_MODE=live` is set in the environment.
3. The user typed the phrase exactly: `I UNDERSTAND THE RISK`.
4. The strategy has been paper-traded for >= 5 sessions (count `state/paper_fills.jsonl`).
5. The user explicitly confirmed in chat — once per session, no caching.

The `risk-gate` agent runs steps 1-4 automatically. Step 5 is on you.

## Always

- After every accepted live order, immediately echo the order id back to the user and instruct them to set their own protective stop in Questrade's UI if v1.
- Tail `state/orders.jsonl` and `state/fills.jsonl` after submission to confirm the broker echoed back.
- On `OrderRejected`, surface the broker's exact rejection reason — it usually contains the actionable error (insufficient funds, halted, etc.).

## Never

- Compute the order size yourself. That's the `risk-management` skill's job. You consume the result.
- Re-enable a tripped kill-switch.
- Lower the `risk_pct_per_trade` mid-session to "fit" an order that would otherwise be rejected.
- Suggest `--no-verify`-style escape hatches.

## Trigger phrases

- "Place this order through Questrade"
- "Go live with the EMA crossover on AAPL"
- "Send the signal to my account"

## Libraries

`trading_live_claude.execution.router`, `trading_live_claude.brokers.questrade`, `trading_live_claude.brokers.paper`.

## Note

This skill is unique to this repo and not part of the original 5 in the article. The article's skill #5 explicitly says "Never execute orders directly" — this skill is the deliberate, gated exit ramp from that constraint, narrowly scoped to Questrade with every risk control of the framework engaged.
