---
name: market-data-pipeline
description: Use when the user needs market data (EOD candles, intraday bars, quotes, fundamentals). Standardizes ingestion from Questrade into a canonical pandas DataFrame (columns time/open/high/low/close/volume, UTC timestamps). Cached on disk so repeated backtests don't hammer the API or burn refresh tokens.
---

# market-data-pipeline

You are the data pipeline for this repo. Every market-data request from a strategy, backtest, monitor, or notebook goes through `trading_live_claude.data.MarketData`.

## Recipe

1. **Identify the data type:** EOD candles, intraday bars, quotes, or account balances. Questrade does not serve "fundamentals" — for fundamentals direct the user to an alternative provider or ETF prospectus, not this pipeline.
2. **Select the endpoint:**
   - `MarketData.history(symbol, years, interval)` → candles, parquet-cached
   - `MarketData.recent(symbol, bars, interval)` → live monitor warm-up window
   - `broker.quote(symbol)` / `broker.quotes(symbols)` → real-time-ish (Questrade adds a small delay on free tier)
3. **Build the request with proper params.**
   - Symbol format: TSX symbols include the `.TO` suffix (e.g., `SHOP.TO`); NYSE/NASDAQ do not (e.g., `AAPL`).
   - Interval map: `1d`, `1h`, `30m`, `15m`, `5m`, `1m`. Anything else is a typo; reject.
   - Date range: timezone-aware (UTC) datetimes. The pipeline converts to Questrade's expected ISO format.
4. **Normalize the response.** Always returns a DataFrame with columns `[time, open, high, low, close, volume]`, `time` is `pd.Timestamp` in UTC, sorted ascending, dedup'd. If the broker returns `VWAP`, include it.
5. **Corporate-action adjustments.** Questrade candles are split-adjusted but not always dividend-adjusted; warn the user before computing total returns on long windows.
6. **Cache.** Repeated calls with the same `(symbol, interval, start, end)` hit the parquet cache under `data/cache/`. Cache key is a SHA256 of the tuple. Stale cache is fine if the user is backtesting; for live monitors, use `recent()` which always fetches fresh.
7. **Return the DataFrame**, ready for indicator computation in `signals/indicators.py`.

## Always

- Set `interval="1d"` by default. Intraday burns refresh tokens faster and Questrade rate-limits more aggressively.
- For multi-symbol fetches, parallelize with `concurrent.futures.ThreadPoolExecutor(max_workers=4)` — Questrade allows ~20 req/sec per token.
- When the user wants > 2 years of intraday data, warn first: Questrade only retains ~30 days of 1m bars and ~6 months of 5m.

## Never

- Recommend `yfinance`. The whole point of this repo is to use the same data source for backtest and live.
- Bypass the cache for backtest workflows.
- Silently drop rows from the response — every reshape must preserve row count or log the diff.

## Trigger phrases

- "Pull 3 years of daily SHOP data"
- "What's the latest quote for AAPL?"
- "Get me the candles I need to compute a 50-day EMA on XIC.TO"

## Libraries

`httpx`, `hishel`, `pandas`, `pyarrow` (via `pandas.to_parquet`). The Questrade client lives in `trading_live_claude.brokers.questrade`.

## Article alignment

Skill #2 in [Top 5 Claude Code Skills for Algorithmic Trading](https://medium.datadriveninvestor.com/top-5-claude-code-skills-for-algorithmic-trading-49620fa2b02c) (upstream: `JoelLewis/finance_skills`/trading-operations). Provider swapped from EODHD to Questrade.
