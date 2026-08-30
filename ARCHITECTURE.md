# FRM-Claude — Architecture Synopsis (for infographic generation)

> **Use this file as the source for an infographic.** It describes a multi-strategy, multi-asset,
> multi-scoring algorithmic-trading platform. Suggested visual: a top-to-bottom **signal-flow
> schematic** — data enters at the top, becomes a risk-gated order at the bottom, with three
> research "tributaries" merging into the scoring stage and a live intelligence overlay clamping
> exposure across five asset classes. Palette suggestion: one warm accent (amber) for the core
> pipeline, cool indigo/teal for research lanes, red reserved for the two control points (risk gate +
> router). Keep module names in a monospace font.

## What it is (one line)
Ideas → signals → scoring → portfolio → **one risk-gated router** → broker. Paper-mode by default;
live trading is a human-only decision behind a typed confirmation.

## Headline numbers
- **115** Python modules · **~13,500** lines · **525** tests passing
- **21** strategy families · **5** asset classes (equity · future · commodity · FX · crypto)
- Python 3.13 · uv · ruff · mypy --strict · httpx · pydantic v2 · structlog · pytest+respx

---

## The core pipeline (8 stages — the vertical spine)

1. **Data ingestion** — market feeds → OHLCV, L2 order books, fundamentals.
   `questrade` · `kraken_ohlc` · `kraken_l2` · `coinbase_l2` · `bitstamp_l2` · `fundamentals_edgar` · `market` · `cache`
2. **Signals & models** — features, strictly no-lookahead (indicators shifted before comparison).
   `signals.generator` (no_lookahead) · `indicators` · `candlesticks` · `valuation` ·
   `models.kalman` · `cointegration` · `timeseries` (ARIMA/GARCH) · `regime` · `selection` (GBT) ·
   `cross_sectional` (ranker) · `pruning`
3. **Strategy families** — 21 registered, composable.
   mean-reversion (`rsi2_connors`, `zscore_ou`, `bb_rsi_combo`) · momentum (`ts_momentum`, `dual_ma`,
   `high_52w_breakout`) · volatility (`atr_channel`, `bbwidth_squeeze`, `vol_target`) · seasonality
   (`turn_of_month`, `day_of_week`, `month_of_year`) · `pairs`/`kalman_pairs` · `macd`/`bollinger`/
   `ema_crossover` · `composite` · `valuation` · `overlay`
4. **Scoring & selection** — swappable objective; recall→precision selection.
   `scoring.objective` (adapter) · `scorer` · `selection` · `routing` · `qc_bridge` ·
   `analysis.classification` (recall/precision) · `matrix` · `roc` · `labeling` · `fidelity` · `universe`
5. **Portfolio allocation** — a correlation-aware, regime-scaled book.
   `allocator` (risk-budget ∝ score · inverse-vol · correlation de-crowding · water-fill caps ·
   regime-scaled gross) · `pipeline` (ranker → book)
6. **Risk gates** *(control point — red)* — `kill_switch` · `sizing` · `heat` · `var` · `tail` ·
   `hedge` · `risk_model` · `allocation`
7. **Execution router** *(control point — red; the single bottleneck)* — every order intent crosses
   `execution.router.Router`. `asset_router` · `daily_budget` · `journal`
8. **Brokers** — `questrade` · `paper` · `kraken_auth` · `token_store`

---

## Live intelligence overlay (WorldMonitor OSINT) — clamps exposure across 5 asset classes

A **live-only** OSINT layer (news alerts, cross-source signals, a geopolitical strategic-risk index,
market fear/greed, VIX, energy stress) normalized into a snapshot and scored into a **per-asset-class
gross-exposure scalar** for **equity · future · commodity · FX · crypto**.

- **De-risk only.** It can trim size and halt new entries; it never adds exposure or generates an
  entry. It multiplies gross at the same seam as the market-regime scalar.
- **Not backtestable by design.** No point-in-time history → using it as a signal would be lookahead;
  it is a live gate/overlay. Every read is journaled to build our *own* point-in-time history.
- **Wired into the live monitor** (`monitor.live_loop`): entries are gated, exits are never blocked.
- **Validated against realized returns:** harder de-risk lands on higher-realized-vol classes
  (rank correlation −1.0 in a live run), and applying the overlay cut a 50-asset book's ex-ante
  volatility from **12.6% → 6.6% (−48%)**.
- Modules: `intel.worldmonitor` (async MCP client) · `overlay` (pure scoring) · `apply` · `routing`
  (symbol→class + refreshing provider) · `chart`. CLI: `intel-overlay`, `signal --intel-overlay`.

---

## Research lanes (three tributaries feeding the scoring stage)

- **Microstructure & execution** — live L2 books, stochastic-control market-making, cross-venue
  arbitrage (all against a simulated order book).
  `microstructure.simulator` · `avellaneda_stoikov` · `orderbook` · `cross_exchange` · `interlisted` ·
  `live_market_maker`
- **ML alpha layer** — gradient-boosted cross-sectional ranker on a 157-name universe; purged
  walk-forward, fundamentals-enriched, wired into the allocator.
  `models.selection` (GBT) · `cross_sectional` · `pruning`
- **Betting analytics** — de-vigged consensus fair line → +EV Kelly value bets, plus cross-book
  arbitrage. Pure functions of odds; a licensed feed plugs in later.
  `betting.odds` · `value` · `arbitrage`

---

## Foundation & ops
- **Backtest** — vectorized engine with a real cost model (commission + slippage + half-spread), so
  every number is net of friction. `backtest.engine` · `costs` (CostModel) · `metrics`
- **Monitor & daemon** — alerts-only polling loop and an autonomous daemon, both fully gated,
  dry-run by default. `monitor.live_loop` · `alerter` · `daemon` · `cli`
- **Integrations** — QuantConnect / LEAN research bridge for multi-asset backtesting.
  `integrations.quantconnect` · `lean_algorithm` · `qc_library`

---

## Non-negotiables (the rules the whole system obeys)
1. **No lookahead** — indicators shifted before comparison; a regression test fails on any leak.
2. **Net of cost** — walk-forward + friction on every claim; in-sample scores treated as suspect.
3. **One gate** — all order intents cross `execution.router.Router`; tests use the paper router, never a live broker.
4. **Human owns live** — secrets stay in `.env`; going live needs a typed confirmation phrase.
5. **Live intelligence de-risks only** — the OSINT overlay can cut exposure, never add it.
