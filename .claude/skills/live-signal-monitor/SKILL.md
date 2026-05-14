---
name: live-signal-monitor
description: Use when the user wants real-time signal detection and alerting on a watchlist of symbols. NEVER places orders directly; it only logs and alerts on entry/exit triggers. Bridges research strategies to live operation in a read-only manner. For order placement, defer to the questrade-execution skill.
---

# live-signal-monitor

You run the polling loop that detects entry/exit signals in near-real-time and emits alerts. You never place orders. Article skill #5 verbatim: "Never execute orders directly; output signal only."

## Recipe

1. **Fetch real-time quotes** for each symbol via `broker.quote(symbol)`. On the free Questrade tier this comes with a small delay (~20s). Document this in the alert footer.
2. **Maintain a rolling window of recent EOD bars** in memory via `MarketData.recent(symbol, bars=required_history_bars+5)`.
3. **Recompute indicators on each new bar.** Re-running the strategy's `generate_signals` over the rolling window is intentional — vectorized is fast enough that incremental updates aren't worth the complexity.
4. **Evaluate signal conditions** by reading the *last row* of the resulting DataFrame: `entry==1`, `exit==1`, or neither.
5. **On trigger,** log timestamp, symbol, price, and indicator values. Append to `state/signals.jsonl`.
6. **Send the alert** through the `Alerter` (print + optional Telegram + optional email). Failure of an alert channel must never raise.
7. **Never call `broker.place_order` from this skill.** The router enforces this anyway via `mode="dry-run"`.

## CLI invocation

```
uv run trading signal --strategy <NAME> --symbols AAPL,MSFT,XIC.TO --interval 60 --iterations 0
```

For a single-snapshot probe: `--iterations 1`.

## Always

- Default polling interval: 60 seconds. Anything tighter starts hitting Questrade rate limits.
- Warn the user if their list has > 25 symbols — quotes endpoint becomes slow.
- Truncate Telegram messages to < 4000 chars (Telegram's hard limit).
- Use UTC timestamps in logs; render Toronto-local in alerts.

## Never

- Place orders.
- Persist secrets in logs (Questrade token, Telegram token).
- Suggest "let me just add the order placement here" — that belongs in the `questrade-execution` skill.

## Trigger phrases

- "Watch SHOP, AAPL, and XIC for EMA-cross signals"
- "Send me a Telegram when RSI on MSFT crosses 30"
- "Run the monitor every 5 minutes"

## Libraries

`httpx`, `pandas`, `time`, `datetime`, in-repo `trading_live_claude.monitor`.

## Article alignment

Skill #5 in [Top 5 Claude Code Skills for Algorithmic Trading](https://medium.datadriveninvestor.com/top-5-claude-code-skills-for-algorithmic-trading-49620fa2b02c) (upstream: `roman-rr/trading-skills`/trading-signals).
