# Risk Disclosure

**Read this entire document before enabling `EXECUTION_MODE=live`.**

## 1. You will lose money sometimes

Algorithmic trading is not a money printer. Most retail algo strategies underperform a passive index after fees, slippage, and taxes. Plan to lose your capital.

## 2. Bugs in this code can drain your account

This is open-source software written and orchestrated by an AI agent. It has bugs you have not found yet. Specific failure modes you must defend against:

- **Stale signal**: indicator computed on yesterday's data triggers an order today
- **Double-fire**: monitor loop restarts and re-emits an already-executed signal
- **Quantity rounding**: `1.7 shares` rounded to `2` exceeds your sizing budget
- **Currency confusion**: a CAD-denominated order sized off USD equity
- **Symbol collision**: `RY` (Royal Bank Canada) ordered on US side as `Royal Caribbean`
- **Token expiry mid-session**: refresh fails, monitor keeps emitting signals but no orders fill (or, worse, fills retry-bomb)

The repo enforces hard gates — risk %, daily loss limit, max positions, kill switch — but none of those are a substitute for you reading the code and verifying the strategy in `practice` first.

## 3. Hard gates enforced by the router

Every live order must pass **all** of these before being sent to Questrade:

| Gate | Default | Configured in |
|---|---|---|
| Per-trade risk % | ≤ 1% of equity | `RISK_PCT_PER_TRADE` |
| Total portfolio heat | ≤ 5% of equity | `PORTFOLIO_HEAT_CAP` |
| Open positions | ≤ 5 | `MAX_OPEN_POSITIONS` |
| Min ticket size | ≥ $100 USD equivalent | `MIN_TICKET_USD` |
| Daily loss limit | -3% halts new entries | `DAILY_LOSS_LIMIT_PCT` |
| Drawdown kill switch | -10% from peak halts everything | `MAX_DRAWDOWN_KILL_SWITCH` |
| Trading hours | Only during regular session | hard-coded per exchange |
| Symbol whitelist | Only `DEFAULT_SYMBOLS` | `.env` |

A gate failure raises `OrderRejected` and the router logs but does not place.

## 4. The kill-switch

`uv run trading kill` writes `state/HALTED` and cancels any open orders. The live router refuses to place new orders while that file exists. Remove it manually after investigation.

## 5. Practice account first

Questrade offers a free practice account. Set `QUESTRADE_ENV=practice` and run for at least **two trading weeks** before touching live. Treat any week with an unexpected error as a reset of the clock.

## 6. You confirm every live session

`trading live` refuses to run unless you pass `--confirm "I UNDERSTAND THE RISK"` as an argument **and** `EXECUTION_MODE=live` is set in the environment. There is no way to remove this — by design.

## 7. Not investment advice

This software does not give financial advice. The included strategies (EMA crossover, RSI mean-reversion, MACD, Bollinger, momentum breakout, pairs) are textbook examples. They are likely **not** profitable out-of-sample at retail scale after costs. They exist to demonstrate the framework, not as recommendations.
