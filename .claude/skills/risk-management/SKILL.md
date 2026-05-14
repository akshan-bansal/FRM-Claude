---
name: risk-management
description: Use any time the user wants position sizing, stop placement, portfolio exposure, drawdown protection, or kill-switch behavior. Wraps the framework's ATR-based fixed-fractional sizer, Historical VaR calculator, portfolio heat tracker, and file-sentinel kill switch. NEVER recommends position sizing by hand or by gut feel.
---

# risk-management

Your job is to compute a *safe* order intent from an equity figure, an entry price, and a risk budget, then verify it doesn't violate the portfolio's overall risk posture.

## Recipe

1. **ATR-based stop distance.** `atr_value = atr(df, window=14).iloc[-1]`. Stop distance defaults to `2 * atr`.
2. **Fixed-fractional sizing.** `dollar_risk = equity * risk_pct`. Default `risk_pct = 0.01` (1%). Cap at 2% even if the user asks for more — refuse a higher value and explain why.
3. **Check portfolio heat.** Sum the `|qty * price|` of open positions. If `total_exposure / equity > 0.05`, refuse to open a new position; tell the user which existing position to consider exiting first.
4. **Compute Historical VaR (95%).** Use the last 252 daily returns. Report VaR as a positive percentage representing the loss threshold not exceeded with 95% confidence.
5. **Output:** `entry, stop, shares (integer floor), dollar_risk, r_multiple_target`. R-multiple target defaults to 2R.
6. **Kill-switch.** Read `state/HALTED`. If present, refuse the order; tell the user to run `uv run trading clear-kill --ack "I HAVE INVESTIGATED"` after investigation. Do not run that command for them.

## Code path

```python
from trading_live_claude.risk import PositionSizer, PortfolioHeat, KillSwitch, historical_var

sizer = PositionSizer(risk_pct=0.01, atr_multiple=2.0, target_r=2.0)
result = sizer.size(equity=equity, entry=last_close, atr_value=atr_value, side="long")
heat = PortfolioHeat(cap_pct=0.05).snapshot(equity=equity, open_risk_dollars=existing_risk + result.dollar_risk)
var_95 = historical_var(returns_series, 0.95)
ks = KillSwitch(Path("state")).state()
```

## Always

- Floor shares with `math.floor(dollar_risk / stop_distance)` — never round up.
- Verify `stop < entry` for longs (`stop > entry` for shorts) before placing.
- If `result.shares == 0` (risk budget cannot afford a single share at this stop distance), tell the user the trade is too risky for this equity and stop there. Do not "round up to 1 share".
- When ATR is unusually small relative to the stock's normal range (e.g., post-halt), warn that the stop may be inside the bid-ask spread.

## Never

- Bypass `Router.submit()`. Even when you compute a perfectly-sized intent, only the router places.
- Recommend a `risk_pct > 0.02`. Hard refusal.
- Suggest disabling the kill-switch as a way to keep trading after a drawdown.

## Trigger phrases

- "How many shares should I buy of X at $50 with a $100k account?"
- "What's my Value-at-Risk on the current portfolio?"
- "Why won't this order go through?" (probable cause: heat / kill-switch / min-ticket)

## Libraries

`numpy`, `pandas`, in-repo `trading_live_claude.risk`.

## Article alignment

Skill #4 in [Top 5 Claude Code Skills for Algorithmic Trading](https://medium.datadriveninvestor.com/top-5-claude-code-skills-for-algorithmic-trading-49620fa2b02c) (upstream: `JoelLewis/finance_skills`/wealth-management).
