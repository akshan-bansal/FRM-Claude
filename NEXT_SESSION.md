# Next-session backlog

**Status (2026-09-08): DATA-ACCRUAL PHASE.** Live paper venues (QT + Kraken) continue to fill
journals on demand; graph journal poll running at 30-min cadence for intel corpus depth. All
commits on `feat/multi-scoring-attention-map`. Standing constraints unchanged: new research
clears the **walk-forward gate** before being tagged validated; live orders stay behind the
**human go-live confirmation**; use `httpx` (not `requests`); `ruff` + `mypy --strict` +
`pytest` must stay green. **Standing operational rule: DATA-FIRST — accrue before tuning,
promoting, or wiring new gates** (see section below for the concrete corollaries).

## Live processes (as of 2026-09-08)

Human-in-the-loop; sessions started + stopped as needed rather than persistent. Current shape:

1. **Kraken paper** — `paper_kraken.py` over the 13-pair CRYPTO_SLEEVE (10 tradeable + 3
   observers post SOL/ADA/POL/UNI/AAVE/ZEC expansion + ZEC/POL/AAVE promotion 2026-09-05).
   Full wiring: OSINT crypto scalar × interpret filter × correlation-aware allocator bias ×
   per-poll MTM × Telegram alerts × fills→graph via `traded` predicate.
2. **QT paper** — `signal --paper --intel-overlay --level` over 15 equities (ARX.TO removed
   2026-09-08 for silent 404 on candles; RSI.TO + RIG.TO added — RIG.TO also 404s, drop
   next session). Same wiring shape as Kraken paper.
3. **Graph journal poller** — `graph_journal.py --iterations 48 --sleep 1800 --held-scope`
   (30-min cadence, 24h coverage per session). Persistence + wash + thesis alerts;
   held-scope filter restricts to WF-validated equities + CRYPTO_SLEEVE.
4. **Dashboard refresh** — `dashboard.py --refresh 300`. Static HTML at
   `reports/dashboard.html` with 9 sections; unchanged.

## Journals (current depth as of 2026-09-08)

- `state/intel_graph.jsonl` — **7,404 edges** spanning 2026-09-01 → 2026-09-08 (7 days
  post-wash on 2026-09-08 morning; the wash pruned ~10.6% of redundant edges).
- `state/intel_overlay.jsonl` — **163 snapshots** spanning 2026-08-29 → 2026-09-08 (10 days).
- `state/paper_fills.jsonl` — **87 fills** across 34 distinct sessions (all-time).
- `state/paper_orders.jsonl` — 86 orders (83 accepted / 3 rejected).
- `state/paper_equity.csv` — 375 MTM rows across all sessions.
- Real QT + Kraken accounts untouched (paper-only path throughout).

## Recent shipments

**2026-09-05 — commit `638f8d3`:**
- Asset-class calibration layer (`analysis/calibration.py`) — CalibrationProfile per
  OverlayClass + `calibrate_for(strategy, symbol)` translation. tune.py wires it; strategies
  unchanged. Widens crypto Bollinger n_std, tightens FX; scales windows to half-life.
- ConfirmOverlay(symbol=…) filters gap-dependent candlestick patterns for 24/7 markets.
- MarketData cache-boundary fix — `end` floored to interval so cache actually hits (was
  regenerating key every call). Interval lexicon standardized on IB/Kraken words; Questrade
  translates `ThirtyMinutes → HalfHour` on its own side.
- PositionSizer conviction ceiling raised [0,1] → [0,3.0] to match `weight_bias` cap in
  live_loop; allocator boosts above 1.0 no longer silently discarded.
- CRYPTO_SLEEVE expanded 7 → 13 pairs (added SOL/ADA/POL/UNI/AAVE/ZEC; POL/AAVE/ZEC promoted
  with baseline scores on 2026-09-05 pm; MKR dropped as Kraken doesn't list the pair).
- `graph_journal.py --pools {equity,crypto}` filter added.

**2026-09-04 — commits (see prior sessions):**
- Resweep survivors PROMOTED: ENB.TO, XIU.TO, VDY.TO, SLF.TO to tier=robust (commit `c3a0d18`).
  Pool now 25 robust + 7 watch. CGL.TO + DBA reclassified equity → commodity.
- WF_PROTOCOLS per-class registry (commit `75e3e6a`).
- OOS win rates BACKFILLED on 27 of 32 validated names (commit `d4a7022`).
- Notification formatter with WF evidence + sizing chain + win rate (commit `78576aa`).
- Telegram plain-text mode (was 400 on Markdown-parsed rich alerts).
- IB-paper feedback-loop gaps 1-6 closed (venue tag, fills→graph, overlay classes for
  fixed_income + precious_metals, futures front-month picker, session tickle warnings,
  persistence gate).
- Cross-path wiring Tiers 1-3 done (OSINT + interpret + allocator into sizing chain;
  fills → graph `traded` predicate; PersistenceGate on domain-elevated entries).
- KrakenBroker adapter live (commit `dff46ff`), IBWebBroker live (commit `a6d008f`).
- Deeper crypto history pipeline + walk-forward crypto script built (commit `cee64b3` +
  shallow-fallback close 2026-09-04); today's 2026-09-08 run validated the WF protocol
  across all 13 sleeve pairs.
- FX pair-trading rejected as untradeable in framework (explicit user decision).
- FX single-name sleeve tried + dropped 2026-09-05 (0 fills / 14 polls empirical).
- Kraken paper OSINT + interpret + allocator bias wiring; QT allocator bias — commit `f9acdcc`.
- Static HTML dashboard (commit `ef28ef1`); Live MTM on PaperBroker (commit `fce41a3`).
- Rename `thicken_graph.py` → `graph_journal.py` (commit `3dc11fe`).

## Runbook — resume from cold

```
# 1. Full test suite
python -m pytest tests/ -q --no-cov --ignore=tests/test_quantconnect.py

# 2. Refresh cache if stale (skips already-cached names)
python scripts/warm_cache.py --held --seed equity --years 5

# 3. Paper monitors (relaunch)
python -m trading_live_claude.cli signal --strategy bollinger \
    --symbols "EQB.TO,QQQ,XIC.TO,ZEB.TO,CGL.TO,VALE,DBC,SRU.UN.TO,CRT.UN.TO,ENB.TO,XIU.TO,VDY.TO,SLF.TO,RSI.TO" \
    --strategy-map "EQB.TO=ts_momentum,QQQ=ts_momentum,XIC.TO=rsi_meanrevert,ZEB.TO=atr_channel,CGL.TO=atr_channel,VALE=bollinger,DBC=bollinger,SRU.UN.TO=rsi_meanrevert,CRT.UN.TO=rsi_meanrevert,ENB.TO=bollinger,XIU.TO=bollinger,VDY.TO=ts_momentum,SLF.TO=bollinger,RSI.TO=bollinger" \
    --interval 300 --paper --paper-equity 100000 --level --intel-overlay
# Note: ARX.TO removed 2026-09-08 — Questrade returned HTTP 404 "Symbol not found" on
# every candle fetch (id=6291), even though symbols/search resolves. Likely a data-
# availability quirk on ARC Resources. RSI.TO (Rogers Sugar) and RIG.TO added same day.
# Note: RIG.TO removed 2026-09-09 — same 404-on-candles / resolves-on-search quirk
# (id=15164671). 20 monitor.step.error rows across a single ~1h session before removal.
# RSI.TO retained (candles work fine). See Gap 1 in the symbol-mapping architecture
# queue for the pre-flight validation that would have caught both before launch.
python scripts/paper_kraken.py --interval 300 --paper-equity 100000

# 4. Graph journal + dashboard
python scripts/graph_journal.py --iterations 96 --sleep 900 --wash-min-hours 72 --persistence-threshold 5
python scripts/dashboard.py --refresh 300
```

## What's queued and unfixed

Note: IB-paper feedback-loop gaps 1-6 all closed 2026-09-03 → 2026-09-04. Cross-path wiring
Tiers 1-3 closed. Remaining Cross-path work (Tiers 4-5) blocks on data accrual — see the
data-first sequencing section above.

- **Cross-path Tiers 4 + 5** — Realized P&L → thesis calibration; prediction evaluation.
  Both need weeks of accrued paper fills + thesis history. 7 days accrued / ~30 days
  minimum for a diagnostic-scale run. **Data-blocked, do not build now.**
- **`enrich_with_agents`** — built + tested + never called. Held pending Anthropic Console key.
- **IB OAuth 1.0a for CP Gateway auth-skip — queued 2026-09-04.** User confirmed OAuth1 flavor
  (the Third-Party API path — RSA-SHA256 signed requests + Diffie-Hellman key exchange for a
  Live Session Token, then LST-signed HMAC-SHA1 for `/v1/api/*` calls). Real build, ~4-6 hrs.
  **BLOCKED on user-side setup** — needs the following artifacts before code can start (all
  from IBKR Client Portal → Settings → OAuth Access → Configure Third-Party API):
    1. `IBKR_OAUTH_CONSUMER_KEY` — assigned when the app is registered
    2. `IBKR_OAUTH_TOKEN` — one pasted this session as `e0d75b4c5c1d2c0f2af7` **must be rotated
       first** (it appeared in the session transcript at
       `.claude/projects/C--Users-PC-Downloads-FRM-Claude/bac3334b-*.jsonl`, session-local but
       persistent). Rotate → new token → put in `.env` (never chat).
    3. `IBKR_OAUTH_TOKEN_SECRET` — paired with the token
    4. `IBKR_OAUTH_SIGNING_KEY_PATH` — path to a local RSA-2048 private key `.pem` whose public
       half is uploaded to IBKR (`openssl genrsa -out ibkr_signing_key.pem 2048`)
    5. Confirmed DH prime + generator from IBKR's OAuth docs (public constants, baked in code)
  Build shape:
    * New `OAuth1Auth` class in `src/trading_live_claude/brokers/ib_web.py` implementing
      `IBWebAuth` interface
    * Live Session Token exchange + HMAC-SHA1 request signing
    * New `--auth oauth1` flag on `scripts/paper_ib.py` (default stays `browser` for backward
      compat)
    * 4 new secret fields in `settings.py` (all default empty)
    * respx-mocked tests for the DH + signed-request flow
    * Update `.env.example` with the field names
  Semantics: this REPLACES the manual browser-login step on CP Gateway. CP Gateway (the Java
  daemon) still needs to be running — OAuth just handles authentication automatically instead
  of requiring the click-through login page.
- **Liquidity-gated entry/exit — queued behind the accumulator.** 2026-09-05 forward decision:
  once the rolling `state/liquidity_hourly.parquet` accumulator has deep-enough coverage
  (~20 obs/cell target, ~140 days), turn the heatmap into a live gate. Design:
  * New `LiquidityGate` primitive mirroring `PersistenceGate` shape — refreshes daily from
    the accumulator, callable `(symbol, ts) → (mult, reason)` returning a size multiplier
    in `[0.1, 1.0]` per that symbol's (hour, weekday) z-score.
  * Wired into `LiveMonitor.step()` alongside `overlay_for` / `interpret_for` /
    `weight_bias_for` — applied last so the chain is
    `conviction *= overlay × interpret × weight_bias × liquidity_mult`.
  * Two prototypes: (a) pure trim (cold cells only cut, hot cells no boost — matches the
    "de-risk for period inactivity" framing, safer to launch); (b) full amplification (hot
    cells up to 1.5× within the calibration ceiling of 3.0 — needs WF evidence).
  * Validation prerequisite: compare cell-hot vs cell-cold realized slippage on accumulated
    fills. Only ship the gate if cold-cell slippage is measurably worse (e.g., 2×+ typical).
    Without this, gate risks cutting sizing on a cost proxy that doesn't actually cost.
  * Fail-open on every path: missing accumulator, refresh exception, unknown symbol all
    resolve to mult=1.0. Same discipline as PersistenceGate.
  * Revisit earliest at ~2026-Dec (accumulator reaches ~90 days at 30-obs equivalent for
    fewer cells).

- **Microstructure accumulator — OMITTED 2026-09-09 per user decision.** Build spec
  below kept for historical context; do NOT propose implementing this. Downstream
  LiquidityGate wire-up is stranded as a result — do not propose that either.

- **Microstructure accumulator (historical spec, DO NOT BUILD).** Enables the
  three-stage liquidity chain (accumulator → deeper heatmaps → `LiquidityGate` wiring)
  by starting the durable data-collection layer. All three downstream items block on
  the accumulator existing; today they run on one-shot 30-90d snapshots.

  **Script:** `scripts/microstructure_accumulator.py` — a slim per-hour cron/scheduled
  task that appends one row per (symbol, timestamp) to
  `state/microstructure_hourly.parquet`. Not a paper monitor, not a graph writer — a
  narrow single-purpose data-collection loop.

  **Storage schema** — one parquet file, appended row-by-row, one row per (symbol, ts):
  ```
  ts:            datetime64[ns, UTC]      # hour boundary (floor of collection time)
  symbol:        string                   # routed form ("BTC/USD", "SPY", "XIC.TO")
  broker:        string                   # "kraken" | "questrade" | "ib_web"
  hour_volume:   uint64                   # trades in the hour
  hour_open:     float                    # first trade / OHLC open
  hour_high:     float
  hour_low:
  hour_close:    float
  bid:           float | null             # if available at collection time
  ask:           float | null
  spread_bps:    float | null             # (ask - bid) / mid * 10000
  bid_size:      uint32 | null
  ask_size:      uint32 | null
  ```

  **Sources per broker:**
  * Kraken: `/public/OHLC` at `interval=60` for the sleeve pairs (no auth). One call
    per pair per hour. Sleeve size 13 → 13 calls/hr.
  * Questrade: `markets/candles` at `interval=OneHour` for the equity 14-pool + one
    `markets/quotes` for bid/ask/sizes. Auth already handled via refresh token flow.
  * IB Web: `/iserver/marketdata/history` at `bar=1h period=1d` for STK proxies
    (bonds/metals/commodity ETFs). Needs CP Gateway auth alive.

  **Cadence:** hourly cron (Windows scheduled task or manual keep-alive), fires at
  :05 of each hour so it captures the just-closed prior hour. Deduplication via
  (symbol, ts) unique constraint; re-runs are idempotent.

  **Failure modes** (each recorded in a companion `microstructure_accumulator.log`):
  * Broker down → skip that broker's symbols this hour, retry next.
  * Symbol resolution error (like ARX.TO / RIG.TO) → log once per symbol per day,
    write empty row so gaps in coverage are visible without polluting the parquet.
  * CP Gateway auth expired → warn once, downgrade to Kraken + QT only that hour.
  * Never raises to the shell — the accumulator must survive its own failures.

  **Integration:** `scripts/liquidity_heatmap.py` grows an `--accumulator` flag that
  reads from the parquet instead of doing fresh fetches. `LiquidityGate` (queued in
  the wire-up section above) reads from the same parquet — same data source, one
  refresh path.

  **Rollup:** every night a companion `_rollup.py` computes per-symbol
  `(hour, weekday) → mean/median/std volume` matrices from the accumulator, writes
  `state/microstructure_rollup.parquet`. Consumers (heatmap, gate) query the rollup,
  not the raw log — faster and lets the raw log stay append-only.

  **Estimate:** ~3 hr build (accumulator + rollup + tests + scheduled-task doc).
  Zero data-window blocker; starts collecting on first run.

  **Revisit thresholds** for consumers:
  * Heatmap regeneration: comfortable at ~20 obs/cell → ~90 days rolling
  * LiquidityGate wire-up: ~30 obs/cell → ~140 days rolling
  * Both self-service once accumulator runs.

  **Standing user rule:** the accumulator will need a scheduled task to run hourly
  without a live claude session. Per the `no-unattended-automation-without-consent`
  standing rule, ASK BEFORE registering the scheduled task; do not create it as part
  of the build.

- **Liquidity heat map — rolling accumulation not yet wired.** 2026-09-05 preliminary run
  produced heatmaps on the sparse windows available (crypto ~30d hourly via Kraken cap;
  IB STK/equity/fixed_income/precious_metals/commodity ~90d hourly via IB Web). Per-cell
  observation density on that first pass is 4-5 (crypto) to 13 (IB STK) — enough for a
  visual guide, marginal for stable per-cell statistics. To get comfortable 20-30 obs/cell
  (~140 days everywhere), the honest path is a rolling accumulator:
  * Cron/scheduled hourly append of the latest hour's volume-per-symbol into
    `state/liquidity_hourly.parquet` — one row per (ts, symbol, volume).
  * Kraken side is trivial (no auth, one call/pair/hour); IB side needs the CP Gateway
    24h re-auth loop already known + the tickle machinery (`_TickleThread` in
    `scripts/paper_ib.py`) — probably better run daily as a batched 24-hour fetch than
    hourly to reduce auth pressure.
  * Regenerate heatmap on demand from the accumulator, no fresh fetches needed.
  * Revisit at ~2026-Dec (~90 days) or ~2027-Q1 (~140 days) for the density threshold.
  * FUT class stays blocked on Alt B (TWS socket) regardless of accumulator progress.
  Full heatmap re-generation is bounded (~10s) once the accumulator exists.

- **Liquidity heat map (Option B) — queued 2026-09-04.** Script built as
  `scripts/liquidity_heatmap.py`; syntax-verified but NOT yet executed to avoid competing with
  the running FX + crypto Kraken deep-fetch chains for bandwidth. Produces one PNG per asset
  class (rows=hour UTC, cols=weekday, per-asset normalization) plus a combined markdown at
  `reports/liquidity_heatmap_<tag>.md` with top-3 hot/cold buckets per asset. Data sources:
  IB Web `/iserver/marketdata/history` (bar=1h, period=90d) for equity + fixed_income +
  precious_metals + commodity + futures (FUT front-month); Kraken `/public/OHLC` (interval=60)
  for crypto + FX. Run after the deep-fetch chains complete: `python scripts/liquidity_heatmap.py`.
  Needs CP Gateway auth'd for the IB classes.
- **FX single-name sleeve: DROPPED (2026-09-05, explicit user decision).** After the FX
  pair-trading rejection (below), a small FX single-name sleeve (4 EUR-cross legs on
  bollinger/confirm_bollinger/rsi_meanrevert/candle_hammer) was stood up on paper_kraken to
  collect empirical tuning data. After ~70 min of live polling the sleeve produced 0 fills
  across 14 polls — Kraken FX quotes essentially static at 5-min cadence and every calibrated
  threshold too tight vs the FX daily excursion. `FXSleeveEntry`, `FX_SLEEVE`, the
  `--pool crypto|fx` selector on paper_kraken, and the `fx` option on graph_journal's
  `--pools` were all removed. Do NOT re-propose an FX single-name sleeve unless (a) an FX
  vendor with sub-minute quote resolution is wired in place of Kraken's 5-min OHLC, or (b)
  the calibration signal-statistics honing (see follow-up section above) produces asset-
  class-specific FX thresholds materially looser than the current calibrator defaults.
  Deep-fetched FX parquets stay on disk for reuse (`data/cache/EUR{GBP,CAD,CHF,JPY}_{daily,
  trades}.parquet`). Calibration's `fx` asset-class defaults remain in place — they're
  generic, not sleeve-specific.

- **FX pair-trading: NOT tradeable in this framework (2026-09-04, explicit user decision).**
  Deep parquets built for the 4 EUR-cross legs (2100-2400 bars each, ~5.9-6.5y daily); pairs
  strategy WF'd against three grid iterations — original (window 20-60, entry_z 1.5-2.5), first
  widening (up to window 180, entry_z 1.2), and FX-oriented sub-1σ (entry_z 0.1-0.75). Widest
  grid produced 2-4 OOS trades vs 0-1 on the tighter grids, but every additional trade LOST
  money. Empirical conclusion: cost-drag on retail Kraken-fill assumptions (5-15 bp per round-
  trip) is 25-75% of typical FX daily-cross excursion, so the small mean-reversion edges don't
  survive. Hourly bars would likely make it worse (excursions shrink by √t, cost unchanged).
  **Do not re-propose FX pair-trading in the WF-validated pool** unless (a) a professional-grade
  cost model is wired (spread <1 pip) or (b) `KalmanPairs` (adaptive hedge) is tried in place
  of the current fixed-hedge crossing-trigger strategy. Full reports in
  `reports/fx_pairs_wf_2026-09-04-*.md`. FX deep parquets stay on disk for later reuse.
- **Excluded from selection (2026-09-04, explicit user decision).** The 2026-09-04 overnight WF
  produced 3 robust survivors from the ETF-proxy set — `LQD` (IG corporate bonds), `MUB` (US
  munis), `DBA` (agricultural commodities). The user explicitly rejected adopting these into
  `WALK_FORWARD_VALIDATED` and asked they be omitted from selection. Do NOT re-propose them in
  a future session unless the user brings them back. Full WF results still live at
  `reports/wf_symbols_proxies_2026-09-04.{csv,md}` for reference.
- **Futures continuous-contract pipeline via ib_insync socket API (Alt B).** Queued 2026-09-04
  after Phase 1 probe against CP Gateway proved IB Web does NOT expose expired-contract data at
  all (every `/iserver/secdef/*` path returns "No Contracts retrieved" for historical months;
  `/trsrv/futures` returns forward contracts only). IB's socket API (TWS or IB Gateway binary
  + `ib_insync.IB.reqHistoricalData` with an expired Contract) does support historical bars —
  the Web API is a REST subset that omits it. Path: install TWS/IB Gateway, enable API, rewrite
  `data/ib_futures_history.py` to use `ib_insync` for the per-contract fetch. Phases 2 (calendar)
  and 4 (continuous-series builder) already built and data-source-agnostic — they can be reused
  as-is. Files landed this session: `src/trading_live_claude/data/futures_calendar.py` and
  `src/trading_live_claude/data/futures_continuous.py`. Estimate: 3 hrs (install + rewrite Phase
  3 + smoke test end-to-end on ES).
- **IB sweep — execute the run.** `scripts/sweep_ib.py` is built and CLI-tested. Needs CP
  Gateway auth'd (`https://localhost:5000` browser login) to actually fetch. One command:
  `python scripts/sweep_ib.py`. Writes `reports/ib_sweep_YYYY-MM-DD.{csv,md}`. Bounded work,
  under 10 minutes wall-time for the default universe (~30 ETFs + 5 futures).
- **Item 0 — full resweep** — needs ~50 min of compute after cache warm. Runbook already in item 0
  below. Nothing to code; just run when the block window allows.
- **Item 8 execution — walk_forward_pairs.py** built (2026-09-04, uncommitted). Reads the latest
  `fx_pairs_scan_*.csv`, filters tradeable rows, fetches both legs via `kraken_ohlc`, walks
  forward the PairsZScore grid under the FX protocol (train=504 / test=126 / step=126) with a
  36-combo per-fold search, tiers by the same equity bar (WFE >= 0.5, OOS > 0, >= 10 trades).
  Needs `fx_pairs_scan.py` run first to produce the shortlist. Bounded work (~15s per pair).

**Held (built but off):** the LLM agent layer (`intel/agents.py`, `enrich_with_agents`) stays
inert without `ANTHROPIC_API_KEY` in `.env` and no caller runs it. See item 6.

**Closed in-session (2026-09-04):**
- **IB-paper feedback-loop gap #1 — Alerter wiring on paper_ib.py + paper_kraken.py.** Both
  scripts now build an `Alerter` (same shape as the QT CLI's `_build_alerter`), format entries /
  exits via `intel.notification`, and push them to Telegram + email + stdout on every fill.
  Empty creds fall back to stdout-only so the venue works with or without `.env` keys. IB's
  tickle-thread 20h/23h escalation is now routed through the same `Alerter`, so the queued
  wire from gap #6 is closed too.
- **IB-paper feedback-loop gap #2 — PortfolioAllocator on paper_ib.py.** Mirrors the paper_kraken
  wiring: builds a returns matrix from ~2y daily bars via MarketData (IB → cache), scores each
  name by past-year Sharpe as a screen-score proxy, runs `PortfolioAllocator(max_weight=0.30)`,
  and passes the resulting bias map as `weight_bias_for` on the LiveMonitor.
- **IB sweep script built** — `scripts/sweep_ib.py`. Executes CP-Gateway-side (needs auth); one
  pass over a bond / commodity / precious-metals ETF + FUT front-month universe, computing base
  stats + overlay scalar + interpret matches per symbol. Writes CSV + compact markdown to
  `reports/ib_sweep_<tag>.{csv,md}`. Queued execution moved to the "unfixed" list above.
- **Item 7 — Crypto WF shallow fallback.** `scripts/walk_forward_crypto.py` now falls back to
  `kraken_ohlc(pair, interval=1440)` when the deep-history parquet is absent. Shallow-derived
  rows report tier=`screened+` (better than pure in-sample, thinner than deep-history WF; never
  `robust`). `--no-shallow-fallback` disables to keep the old strict behavior.
- **Item 8 — FX pair-trading discovery MVP.** `scripts/fx_pairs_scan.py` fetches shallow daily
  OHLC for Kraken's most-liquid fiat FX pairs, enumerates `C(n, 2)` combinations, runs Engle-
  Granger cointegration via the existing `analysis/pairs.py::enumerate_pairs`, and reports the
  tradeable shortlist (cointegrated + finite half-life). FX-tuned defaults (`max_half_life=60d`
  vs. 252d for equity) reflect faster mean-reversion. Discovery only — walk-forward wrapper for
  the shortlist is still queued.

**Closed in-session (2026-09-03, later pass):**
- **IB-paper feedback-loop gap #4 — fixed_income + precious_metals classes.** `OverlayClass`
  Literal + `OVERLAY_CLASSES` tuple extended in `intel/overlay.py`; `_compose` grows two new
  branches with class-appropriate risk-off character (bonds get lightly-blended global + economy +
  conflict gates because flight-to-quality usually rallies duration; metals get the full dxy gate
  plus lightly-blended conflict/disaster because they're safe-haven with mild systemic squeeze
  risk). `intel/routing.py` grows `_FIXED_INCOME_SYMBOLS` (TLT/IEF/SHY/BND/AGG/LQD/HYG + XBB.TO/
  ZAG.TO/VAB.TO) and `_PRECIOUS_METALS_SYMBOLS` (GLD/SLV/PSLV/CGL.TO/…), and `classify_symbol`
  now checks them BEFORE the broad-commodity list. Bond ETFs on IB paper no longer inherit the
  equity scalar; metals no longer bucket with oil.
- **IB-paper feedback-loop gap #6 — CP Gateway 24h re-auth warning.** `_TickleThread` in
  `scripts/paper_ib.py` now tracks wall-clock session age and escalates once each at 20h (WARN)
  and 23h (CRITICAL). Any tickle failure whose response looks like an auth expiry (401/403/
  unauthorized) also fires the CRITICAL once. Warnings are plumbed through a `warn_fn` callable
  so when the queued Alerter wiring (gap #1) lands, threading through to Telegram is one line.
- **IB-native futures wire-up.** `IBWebBroker.set_sec_type(symbol, sec_type)` registers a
  per-symbol override; `resolve_conid` routes FUT-type resolutions through `/trsrv/futures`
  (which returns per-expiration conids, unlike `/iserver/secdef/search` which only returns
  roots), picking the front-month = earliest expirationDate strictly after today with a
  fallback to earliest-of-stale if every listed contract already expired. `scripts/paper_ib.py`
  grows `--futures ES,NQ,CL,GC,ZN` which registers each root as FUT before the monitor's first
  quote call. 5 new respx-mocked tests covering front-month pick, stale fallback, unknown root,
  cache invalidation on override, and default STK behavior — all green.
- **Cross-path tier 3 — Graph persistence → entry gate.** `intel/routing.py` grows
  `_CLASS_TO_DOMAINS` (a mapping from overlay class to the graph domains that class is exposed
  to) and `PersistenceGate` (callable, refreshes on cadence, returns `(halt, reason)`).
  `LiveMonitor` grows a `persistence_for` hook mirroring `overlay_for` — an entry in a symbol
  whose class touches a persistently-elevated domain (≥ `min_polls` consecutive polls, default 5)
  is halted (router skipped) with the halt reason landing on the alert. Fail-open on every
  failure path: missing graph, parse errors, refresh exceptions all resolve to "no halt" so a
  broken intel path never causes an unexpected trading halt. 11 focused tests in
  `test_intel_routing.py`.

**Closed in-session (2026-09-03, earlier pass):**
- IB-paper feedback-loop gap #3 — `venue` tag on every PaperBroker journal row. `.venue`
  class-attr on every real broker (`questrade`, `kraken`, `ib`, `ib_web`); `PaperBroker.__init__`
  inherits from the feed and accepts an explicit `venue=` override. `paper_fills.jsonl` and
  `paper_orders.jsonl` now carry the venue on every row so the shared journal is groupable by
  dashboard. Equity CSV schema deliberately unchanged (join by `session_id` to derive venue,
  avoiding a schema break in `state/paper_equity.csv`). Tests in `tests/test_paper_broker_journal.py`.
- IB-paper feedback-loop gap #5 — Fills → intel graph event nodes. Extended `intel.graph` with
  `venue`/`symbol` node types and a `traded` predicate; new `fill_edge(...)` helper composes one
  edge per fill (weight = signed notional; meta carries action/qty/price/session_id/order_id).
  `PaperBroker._journal_fill` appends the edge via `append_edges(...)` in the same call site as
  `paper_fills.jsonl`, in a try/except so a graph-write failure never crashes a trade path.
  Closes what was cross-path tier 2 in the earlier audit — `edge_persistence` can now see fills.

---

## Data-first sequencing — 2026-09-08 standing rule

Explicit ordering for the currencies + equities sleeves: **accrue data before you tune,
promote, or wire new gates**. Applies to every calibration, promotion, or microstructure-
based rule proposed today or later. Concrete corollaries:

* Walk-forward runs (like today's 13-pair crypto WF) serve as PROTOCOL VALIDATION —
  confirming the pipeline scores across the sleeve — NOT as tier-promotion triggers.
  Do not promote a pair to `tier=robust` on a single WF pass; require deep-history
  evidence + multi-fold OOS stability.
* Do not calibrate signal-statistics (Bollinger n_std, ZScore entry_z, RSI oversold)
  until deep-history is available across the sleeve — thresholds tuned on shallow
  720-bar windows over-fit to the current regime.
* Do not wire microstructure-based gates (LiquidityGate, liquidity-heat trims) until
  the accumulator has ≥90 days of density.
* No new autonomy or scheduled tasks in the interim beyond what's already running;
  keep the loop human-in-the-loop while the data corpus deepens.

## Crypto WF protocol — 2026-09-08 validated, promotion held  🟢 PIPELINE OK

Today's `scripts/walk_forward_crypto.py` run scored all 13 currencies-sleeve pairs
cleanly (2 deep / 11 shallow) — pipeline works, tier fields NOT updated per the
data-first rule above. Purpose was protocol validation; outputs recorded to
`reports/walk_forward_crypto.csv` for later cross-reference once deep history lands.

**Diagnostic-only takeaways** (not action items):
* 11 of 13 pairs currently on shallow (~720 bar) Kraken /public/OHLC — WF gives them
  ~3.9 folds and 0-3 OOS trades most sleeves. Insufficient sample for tiering.
* Deep-history-backed pairs (PAXG, BTC) had enough sample to produce meaningful OOS
  numbers but tier assignment held pending broader corroboration.
* Zero-trade rows (SOL/POL/ADA/ZEC/XMR) indicate strategy thresholds probably
  mistuned for those pairs' vol distributions — DO NOT hone thresholds now; wait for
  deep data to confirm the pattern.

## Deep-history fetch (currencies sleeve) — 2026-09-08  🟡 QUEUED

Prerequisite for the crypto WF protocol to produce actionable tier decisions. Current
state: only PAXG + BTC have deep parquets under `data/cache/`. Other 11 pairs run on
shallow 720-bar Kraken fetches.

**Priority order** (deepen the diversifiers + cluster cores first, then the rest):
1. **ETH/USD** — cluster core, missing from deep cache. `python scripts/fetch_crypto_history.py --pair ETHUSD --since 2020 --max-pages 15000`
2. **XMR/USD** — partial diversifier (avg |ρ| 0.39). Same command form, --pair XMRUSD.
3. **ZEC/USD** — genuine diversifier per correlation study (avg |ρ| 0.41). --pair ZECUSD.
4. **LINK/USD** — cluster; the atr_channel strategy shows some signal on shallow. --pair LINKUSD.
5. Remaining cluster: **XRP, XLM, SOL, ADA, POL, UNI, AAVE, MKR** — batch after 1-4.

Each fetch is multi-hour (Kraken /public/Trades pagination at ~1 req/s, ~1000
trades/page). Total wall time for all 13 pairs likely 12-24 hours if run sequentially.
Parallelization risky (Kraken rate limits per API key, not per pair).

Prior 2026-09-04 orchestrator (`scripts/targeted_orchestrator.py`) can be adapted;
the CRYPTO_SLEEVE-driven loop already iterates over `sleeve.values()`.

**Blocker for auto-run:** wall-time + Kraken rate discipline. Recommend a scheduled
overnight run (~03:00 local start) rather than a foreground session. Per the standing
`no-unattended-automation-without-consent` rule, ASK before registering the scheduled
task.

## OSINT × commodity-proxy correlation study — DESIGNED, PARKED  🟡 WAITING ON DATA DEPTH

Designed 2026-09-05, parked same-session on window mismatch. The regression spec is ready
to run; the data corpus is not deep enough for it to produce non-noise results yet.

**Research question:** does OSINT domain elevation (from the 15-min graph poll) systematically
precede forward returns in commodity/futures ETF proxies at 1/5/21-day horizons?

**Regression spec (frozen for later):**
* Response: `log(close_{i, t+h} / close_{i, t})` for h ∈ {1, 5, 21}
* Features per domain d ∈ {energy_stress, conflict, natural_disasters, dxy, safe_haven,
  economy, financial_stress}: raw scalar `s_{d,t}`, 90-day-trailing z-score
  `elev_{d,t}`, consecutive-poll persistence count `pers_{d,t}` (via existing
  `edge_persistence`).
* Controls per proxy i: 21-day momentum `mom_{i,t}`, 21-day realized vol `rv_{i,t}`.
* OLS + Newey-West SE (h-lag overlap induces serial correlation).
* Benjamini-Hochberg FDR at α=0.10 across the 14 × 3 × 7 = 294 individual γ tests.
* Cadence alignment: EOD OSINT aggregate (last poll before 16:00 ET), daily ETF close.

**Symbol list (14 ETF proxies):** USO/UNG (energy), GLD/SLV/PPLT/CPER (metals),
WEAT/CORN/SOYB (grains), TLT/IEF/HYG (rates/credit), UUP (dollar), VXX (vol).

**Blocker:** intel journal too shallow.
* `state/intel_overlay.jsonl` — 143 rows, 2026-08-29 → 2026-09-05 (7 days).
* `state/intel_graph.jsonl` — 8974 edges, 2026-09-01 → 2026-09-05 (4 days).
* No backfill path (WorldMonitorClient is live-only; can't retroactively query historical
  DXY_chg or category_alert_counts).
* 5-day and 21-day horizons: essentially zero observations.
* 1-day horizon: ~7 obs per proxy (98 pooled) against 9-10 parameters — over-parameterized.

**Revisit criteria:** intel journal spans ≥ 3 months (~60 trading days per proxy → ~10:1
obs:param at the 1-day horizon; supports pooled cross-sectional OLS). ≥ 6 months for the
5-day horizon to be defensible; ≥ 12 months for 21-day. So earliest honest revisit is
~2026-Dec (3 mo from now), full-scope revisit is ~2027-Mar.

**Available today without waiting:** ETF proxy historical bars are already cachable via
IB Web STK path — the fetch layer is not the blocker. Only the OSINT feature side is.
Anyone who wants to work on this before the intel corpus deepens should build ONLY the
proxy-side data pipeline + the regression harness (so ~2026-Dec re-run is one command),
NOT run the regression itself with the shallow corpus dressed as findings.

**Related standing decision:** same window-depth logic applies to any other cross-signal
study on the intel graph — calibration signal-statistics honing (section above), thesis-
calibration recurrent-learning loop (item 9 tier 4), prediction evaluation (item 9 tier 5).
All four studies wait on the same underlying corpus.

## Symbol-mapping architecture — 2026-09-08  🟡 QUEUED

Surfaced from a mid-session audit of how the same asset is represented across brokerages.
Each broker adapter normalizes independently (Questrade `_symbol_id()`, Kraken
`to_kraken_pair()`, IB Web `_resolve_stk_conid()`, IB socket `Stock(sym, "SMART", "USD")`)
and the internal codebase uses `analysis.asset_spec.spec_for(symbol)` as canonical
identity — functional for single-broker-per-sleeve, but three gaps have real cost.

### Gap 1: Pre-flight symbol validation (small, high-value)
Both landmines from this week's paper sessions surfaced only at runtime — ARX.TO 404'ing
on Questrade candles across 5+ polls, MKR/USD raising `EQuery:Invalid asset pair` mid-
poll and killing the whole Kraken session via structlog cascade. There is no `validate_
sleeve(broker) -> list[SymbolValidation]` helper that walks every sleeve entry at startup,
attempts a minimal fetch (symbol/search + one bar of candles), and returns per-symbol
status BEFORE the monitor's first poll. ~30 lines to build; would have caught both this
week's failures pre-launch instead of mid-run. Wire into `scripts/paper_kraken.py` and
`cli.py signal` startup banners; refuse to start the loop if any HARD failure (candle
404, invalid pair). WARN on soft failures (e.g., short cached history).

### Gap 2: Cross-broker symbol atlas
No shared table maps the same canonical asset across brokerages. If a Kraken-spot vs
IB-futures basis trade is proposed (or any Kraken spot vs IBIT ETF cross-venue), each
broker needs its own sleeve entry and nothing relates them. `CryptoSleeveEntry` today
carries `.symbol` (routed) + `.pair` (Kraken REST), which is a two-form pattern for
one broker; extending to N brokers grows quadratically without a first-class atlas.

Proposed module `analysis/symbol_atlas.py`:

```
@dataclass(frozen=True, kw_only=True)
class BrokerSymbol:
    canonical_id: str            # "BTC-USD-SPOT" — venue-agnostic identity
    broker: Literal["questrade","kraken","ib_web","ib_socket"]
    wire_form: str               # what the broker's API expects on the wire
    sec_type: str = "STK"        # STK / FUT / CASH / CRYPTO
    meta: dict = field(default_factory=dict)

_ATLAS: dict[str, list[BrokerSymbol]] = { ... }

def resolve(canonical_id: str, broker: str) -> BrokerSymbol | None
def validate_atlas(broker: Broker) -> list[SymbolValidation]
```

Migration path: sleeve entries stop carrying dual-form fields; they carry a
`canonical_id` and the atlas resolves per-broker. Backwards-compatible via a shim on
`CryptoSleeveEntry`. Would also absorb the current `_BOND_REGISTRY` / `_METALS_REGISTRY`
/ `_FUTURES_REGISTRY` scaffolding in `analysis/asset_spec.py` — those are effectively
per-class atlases already, just single-broker.

### Gap 3: IB socket path silent mis-routing
`brokers/ib.py:354` hardcodes `Stock(order.symbol, "SMART", "USD")` for equities.
Canadian ETFs (`XIC.TO`, `VDY.TO`) submitted via socket would be silently mis-routed
(SMART routes US-listed venues only; USD currency wrong for TSX). IB Web adapter fixed
this via `set_sec_type()` override + `.TO` suffix strip; socket path never got parity.

Fix (short): port `_resolve_stk_conid()`'s exchange + currency inference into a helper
called by `brokers/ib.py::place_order`. Preserves the socket adapter's operational
independence but removes the mis-routing hazard. ~40 lines + tests.

### Sequencing
1 (validation preflight — ~30 min) → 3 (IB socket exchange fix — ~1 hr) → 2 (full atlas
+ migration — ~4-6 hr, only if cross-venue trades get proposed). Gap 1 alone would have
prevented both this week's mid-session failures; do it first, atlas can wait.

## Risk-guard validation analysis — 2026-09-09  🟡 QUEUED (needs ≥ 1 week post-gate data)

Empirical audit of items 1/2/3/5 from the 2026-09-08 risk-architecture landing (commit
`629594c`). Purpose: verify each new gate is actually firing as designed, calibrate
thresholds against the observed fill/reject distribution, and surface false-positive vs
false-negative behavior. Item 4 (strategy-level opt-in stops) intentionally excluded —
it's data-blocked pending WF and has no code to audit.

**Data sources (all already journalled):**
* `state/paper_orders.jsonl` — accept/reject decisions with `rejected_reasons` list.
  Query for the 4 new reason strings: `"single-name notional"`, `"gross leverage"`,
  `"kill-switch tripped"`, and forced-exit reasons (in fill journal, not orders).
* `state/paper_fills.jsonl` — records `forced-exit` reasons on the intent side when
  Router.check_forced_exits emits them.
* `state/paper_equity.csv` — post-fill drawdown_pct series feeds the kill-switch
  eval; a KillSwitch trip corresponds to `state/HALTED` appearing in a given session's
  state_dir.

**Per-gate probes:**

1. **Kill-switch tighten + auto-halt (item 1)** — count HALTED sentinels per session,
   correlate with the equity trajectory that triggered them. Compare pre-2026-09-08
   sessions (pre-tightening) vs post — did the 3% threshold trip on sessions the 8%
   threshold would have let ride? Cross-reference against the subsequent MTM: would the
   halted position have recovered? Rough false-positive metric.
2. **Per-symbol notional cap 50% (item 2)** — count `"single-name notional"` rejections
   per session. Retrospect: which name/strategy triggered the cap? Would the intent's
   size have been the trade that hit the leverage cap (the VDY.TO failure mode)? Cap
   binding rate = rejections / total-accepted-entries; want this in the 1-5% range
   (frequent enough to be doing work, rare enough not to strangle the sleeve).
3. **Intra-day forced exit (item 3)** — count forced-exit fills per session, correlate
   with entry-to-exit unrealized loss trajectory. Empirical calibration of
   `force_exit_atr_mult`: at 3.0, is the gate firing on noise (position would have
   recovered) or on real breakdowns (position kept falling)? Sweep candidate values
   {2.0, 2.5, 3.0, 3.5, 4.0} on the accrued fill history.
5. **Portfolio gross-leverage cap 1.0× (item 5)** — count `"gross leverage"` rejections;
   verify the sleeve's max observed gross leverage stays ≤ 1.0. Distinguish from the
   per-symbol cap — leverage cap binds when MULTIPLE positions cumulatively hit the
   ceiling, per-symbol binds on a SINGLE dominant position.

**Cross-gate interactions to watch for:**
* Kill-switch trip triggered by a position the per-symbol cap should have prevented
  from opening → indicates the size cap default is too loose.
* Forced-exit gate firing on positions that were sized within all pre-entry caps →
  indicates the entry gates missed something the exit gate caught (good — defense in
  depth working).
* Same position rejected by BOTH per-symbol AND gross-leverage → indicates the two
  gates are collinear at current defaults; may want to relax one.

**Deliverable:** `reports/risk_guard_analysis_YYYY-MM-DD.md` with per-gate firing rates,
threshold-sensitivity tables (esp. force_exit_atr_mult sweep), and any calibration
recommendations. Uses `state/paper_orders.jsonl` + `paper_fills.jsonl` + `paper_equity.csv`
— no fresh fetches needed.

**Data-window requirement:** at least **1 week of post-2026-09-08 sessions** running both
QT + Kraken paper. Today (2026-09-09) is day 1; earliest honest run is **~2026-09-16**.
Longer window (2-4 weeks) improves the false-positive/false-negative separation. Same
data-first discipline as the OSINT and thesis-calibration studies.

**Not blocked by:** intel corpus depth (the analysis uses paper-broker journals, not intel
graph). Independent of the microstructure accumulator + LiquidityGate build path.

## Risk-architecture follow-ups — 2026-09-08  🟢 4 OF 5 LANDED (item 4 data-blocked)

Prompted by a QT paper session where VDY.TO at ts_momentum × 2.33× allocator boost hit the
vol-target max_leverage=1.0 cap and took 100% of paper equity ($100,044 notional on
$99,995 equity). Loss reached −$416 (~0.42%) with no gate firing — well below the 8.0%
kill-switch and 3.0% daily-loss limit, and no strategy-level stop configured on
ts_momentum. Four separate follow-ups:

### 1. Tighten max-drawdown kill-switch: 8.0% → 3.0%  ✅ LANDED 2026-09-08
`config/trading.yaml::max_drawdown_kill_switch = 0.03`. The current 8.0% threshold is
loose for a paper-validation context — by the time it trips the account has already lost
$8k. 3.0% gives the same margin against real-world overnight-gap noise (~2σ on a broad
equity book) but halts before catastrophic runaway. Router already reads this from
settings so no code change; single config edit + relaunch.

### 2. Dynamically-weighted max_position_notional_pct gate per symbol  ✅ LANDED 2026-09-08 (fixed 0.50 default; vol-weighted variant deferred)
Add a router gate that caps single-symbol notional as a percent of equity, weighted by
the position's own risk contribution (not just size). Shape:

* Per-symbol cap defaults to `1 / max_open_positions` × 1.5 (~50% for the current
  max_open_positions=3), acting as a hard ceiling.
* Weighted by (annual_vol_i / mean_annual_vol_sleeve) so a low-vol name (VDY, PAXG)
  can hold a larger absolute notional than a high-vol name (VALE, SOL) — the point is
  equal risk contribution, not equal weight.
* Runs as a Router gate alongside heat_cap / max_open_positions / daily_loss_limit.
  Rejects the intent (or trims the size to the cap) when a fill would push notional
  above the weighted ceiling.
* Cross-checks against `PortfolioAllocator.max_weight` (0.30 sleeve-level cap) — the
  new gate is per-name inside a sleeve, catches the "one boosted name saturates the
  leverage cap and takes 100% of equity" failure mode the sleeve-level cap misses.

### 3. Move exit checks to intra-day bar cadence  ✅ CHEAP PATH LANDED 2026-09-08 (Router.check_forced_exits); real path (intra-day bar strategy hook) deferred
ts_momentum (and other daily-bar strategies) currently check `generate_signals` on the
daily close only, so a −0.4% intra-day drawdown is invisible until the next EOD bar.
Two paths to fix:

* **Cheap:** router adds a "loss-based exit" gate — if any open position's unrealized
  loss exceeds N × ATR since entry, force-close on the next intent. Runs on the
  monitor's poll cadence (5-min), independent of the strategy's bar.
* **Real:** strategies opt into an intra-day recheck flag; the monitor calls a slim
  `should_exit_intraday(bar, position) -> bool` on every poll, backed by a 1h or 30m
  bar instead of daily. Bigger change; needs the interval-standardization work (already
  landed 2026-09-05) plus a broker-side intra-day fetch pipeline.

Recommend starting with the cheap path (router-level unrealized-loss exit) since it
protects every strategy uniformly and doesn't require per-strategy retrofits.

### 4. Configure strategy-level risk-stops on the exit-less strategies  🟡 DATA-BLOCKED (needs WF)
`strategies.base.Strategy` supports opt-in `stop_atr_mult` / `trail_atr_mult` /
`time_stop_bars` but every strategy in the sleeve except `bollinger` (time_stop_bars=15)
and `candlestick` (stop_atr_mult=3.0) leaves them at `None`. Concrete assignments to add
after walk-forward evidence supports them:

* `ts_momentum` — add `trail_atr_mult=4.0` (Chandelier trailing stop, standard for
  momentum). Chosen over a fixed stop because momentum needs room for pullbacks; ATR
  scales that room by the name's own volatility.
* `rsi_meanrevert` / `bb_rsi_combo` / `zscore_ou` — add `time_stop_bars=20`. A dip
  that hasn't reverted in 20 bars is a stale thesis; force-close and free the capital.
* `atr_channel` / `macd` / `ema_crossover` — add `stop_atr_mult=3.0`. Trend-following
  wants a hard floor; the trailing-stop version (`trail_atr_mult`) is only appropriate
  once WF evidence confirms the strategy captures durable trends.

Each assignment must clear a walk-forward run before landing — an added stop that hurts
the OOS score is a bad trade for peace of mind. Bounded work: rerun tune per strategy
family, keep the assignment only if sortino_over_dd improves.

### 5. Portfolio cash-balance / gross-leverage gate on the router  ✅ LANDED 2026-09-08

### 6. Trim-instead-of-reject on the size-cap gates  🟡 CODE-COMPLETE 2026-09-09, NOT EMPIRICALLY VALIDATED
Behavior change: both size caps (per-symbol + gross-leverage) resize `intent.shares` to
fit the tighter cap and accept, controlled by `on_size_cap_breach: 'trim' (default) |
'reject'` on Router + settings. Falls through to reject only when no headroom remains
OR when trimmed size falls below `min_ticket_usd`.

**What actually got validated today:** the OLD reject-mode gate fired 39x on VDY.TO in
a single QT paper session at 100%-of-equity notional (correcting a false claim earlier
in the day that gates were idle). That validated the ORIGINAL 2026-09-08 gate was
binding correctly.

**What did NOT get validated today:** the new trim mode's runtime behavior. Written +
unit-tested (5 tests, all passing) but not exercised against a live paper session
until the next launch. Do NOT claim "trim prevents the 39-attempt loop" as empirical
until at least one paper session shows the actual trim → accept → position-open
sequence in `paper_fills.jsonl`.

Old reject-mode tests preserved via explicit `on_size_cap_breach="reject"`.

### 7. Alerter dedup — OMITTED 2026-09-09 per user decision.
Do NOT propose changes to the Telegram alerter. The Alerter stays as it currently is.

### 9. Exchange hopping — trading beyond user's geographical timezone  🟡 QUEUED (2026-09-09)

**Levels approved but not built** (queued 2026-09-09). Applies the propagation-notes
discipline: each level's cross-module impact is enumerated so we don't slip an
architectural change in as a small feature.

**Level 1 — Session-hours guard (~2 hr, next step).**
* New `analysis/venue_calendar.py` with market-hours-per-venue + `is_open(venue, ts)`.
* LiveMonitor consults the guard before polling each symbol; symbols whose venue is
  currently closed get skipped, preventing wasteful broker calls + stale-quote signal
  errors during off-hours.
* No new trading enabled — foundation only. QT + Kraken behavior unchanged during
  their live sessions.
* **Propagation:** LiveMonitor.step() adds one gate. Broker adapters get a
  `.venue_calendar` attribute (or free function). No state schema change. Kill-switch
  + heat gates unaffected. Alerter behavior unchanged.
* **Cadence effect:** polls for closed-venue symbols drop from every 5min to zero
  during their off-hours; net API-call reduction on the current pool.

**Level 2 — Multi-venue equity via IB (~6-8 hr + WF).**
* Add Tokyo / London / Sydney / etc. equities to `WALK_FORWARD_VALIDATED`. IB socket
  path's `_infer_stock_venue` already handles suffix routing (landed 2026-09-09).
* Requires IB Gateway + global-market-data subscriptions (real account cost decision).
* WF runs per new symbol before promotion.
* **Propagation:** no code deltas beyond L1 for the routing itself; the additions
  are data (WF pool). Intel overlay's US-centric OSINT feed does NOT surface Asia
  events well — a Nikkei trade uses `equity` class scalar without any Asia-specific
  intel signal. Document that gap when adding.

**Level 3 — Multi-currency accounting (~8-12 hr).**
* PaperBroker records position currency. Equity CSV adds `numeraire_equity` column
  with FX-adjusted totals. FX rates from IB or Kraken's fiat pairs.
* All risk gates switch to numeraire-based math (KillSwitch, PortfolioHeat, allocator
  correlation matrix).
* **Propagation — critical.** FX rate moves become a NEW risk vector: a position
  flat in native currency can trip the drawdown gate through FX alone. The KillSwitch
  daily-loss baseline needs an explicit numeraire choice (USD is conventional but
  CAD may be the user's home currency). Correlation math is only sound in a common
  numeraire — mixed-currency return series bias the covariance matrix by the FX
  volatility of each pair.
* Feedback loop: FX-adjusted equity → KillSwitch.evaluate → potential HALT even when
  book is fine in native currency. Must be tested with an FX-shock scenario before
  live.

**Level 4 — 24-hr scheduler + non-overlapping correlation (~15-20 hr, speculative).**
* True global book, session-aware intent scheduling, correlation smoothing across
  venues with different close times (Kalman filter or overlapping-window rebase).
* Only worth designing after L2 + L3 have live evidence — this level is a
  hypothesis, not a build spec.

**Sequencing gate:** Levels 2-4 depend on real IB account market-data subscriptions
(spend decision) AND on the L3 numeraire choice (architecture decision). L1 is
bounded and independent — worth doing regardless of the higher levels.

### 8. Event + level entry triggers (both, decoupled)  🟡 QUEUED (2026-09-09)
User standing rule: we need BOTH event-based (fresh cross) AND level-based (state
currently satisfied) entry triggers — neither one alone is reliable enough.

Failure modes each covers for the other:
* **Event-only fails when:** the monitor starts after the fresh cross has already
  happened; OR the first-fired intent is gate-rejected (e.g., size-cap trim now
  handles this on-line, but there are other reject reasons where the position never
  opens); OR the fresh-cross bar is skipped for any polling reason. Result: no entry
  until the NEXT cross, which may never come for many bars.
* **Level-only fails when:** the current level is persistent for many polls in a row
  (would re-fire indefinitely, requiring open-position checks to prevent re-entry);
  OR the level is a one-bar spike that closed level-eligible even though the intent
  was "trigger at the crossing".

Contract shape:
* Each strategy emits BOTH `entry_event` (0/1 on fresh cross) AND `entry_level`
  (0/1 while state currently satisfies condition). Strategy authors pick which apply
  to their setup — momentum crossings are event; band-touch dips are level.
* LiveMonitor consumes both columns; per-symbol config picks which to act on
  (default: event; opt-in to level for specific families).
* Router's open-position check prevents re-entry on level-triggered re-fires
  (already handled by the monitor's holding-book, but the check needs to be tight
  in the level path).

Touches every strategy class (adds one column) + LiveMonitor + optionally per-strategy
config. Estimate 3-4 hr + tests. **Explicitly does NOT touch the Alerter** — the
alerter's ergonomics are separate and out of scope per the standing rule above.
Surfaced from the same VDY-concentration session: 3 fills totaled $110,109 notional
against $99,995 starting equity — paper broker allowed cash to go **negative (−$10,124)**
because the sizer's `max_leverage=1.0` is per-position (vol-scale-ceiling), not
per-portfolio. Portfolio gross leverage on that session was 1.10× with zero real
guardrail; a real account would either reject or margin-borrow. Distinct from the
per-name notional cap in item 2 above — that one prevents any single name from
dominating; this one prevents the sleeve as a whole from exceeding available cash.

Router gate shape:

* `max_gross_leverage` config (default 1.0 for paper — accepts no leverage; higher for
  margin accounts once live).
* Pre-fill check: `(sum_open_notional + intent.notional) / equity` must not exceed the
  cap. If breach, either trim the intent size to the remaining headroom OR reject
  outright — config flag `on_leverage_breach = 'trim' | 'reject'`.
* Runs BEFORE the vol-target sizer's leverage-cap-of-1.0 fires, so a name never gets
  sized past what the sleeve can fund. Ordering matters: this gate sees intents in
  submission order, so first-fill wins the remaining headroom.
* Cross-check: `PaperBroker._journal_equity` already computes cash + positions_value
  per row — reuses the same accounting, no double-tracking.

Estimate: ~1.5 hr including test coverage. Would have prevented the −$10k cash breach
outright.

**Dependency ordering:** 1 (config edit — 5 min) → 3-cheap (router unrealized-loss exit
— ~1 hr) → 5 (portfolio leverage gate — ~1.5 hr, catches the more common failure than 2)
→ 2 (per-name notional gate — ~2 hr) → 4 (WF-validated strategy stops — ~4 hr per
strategy family, needs cache warm). None require the intel corpus to deepen.

## Asset-class calibration — signal-statistics follow-up  🟡 QUEUED

Landed 2026-09-05: `src/trading_live_claude/analysis/calibration.py` translates a symbol into
per-class strategy kwargs (window, n_std, oversold, entry_z, exit_ma, atr_window), wired
through `tune.py` and the `confirm_<base>`/`candle_<pattern>` factories. Matrix output on
2026-09-05 confirmed the **time-scale side is sound** — windows track half-life, ema/donchian
scale with `bar_scale`, exit_ma tracks the 0.7·HL rule.

**What still needs honing (the microstructure-signal side):**
- `n_std` on Bollinger — currently a simple `vol/0.20` multiplier around a regime-based
  base (2.0/2.5/1.8). Doesn't yet reflect that crypto's fat-tail distribution needs a
  higher-percentile band than the same annualized-vol gaussian would predict; FX majors'
  tight-spread microstructure means the 1.44 band probably over-triggers on cost.
- `entry_z` on ZScoreOU — currently `1.5 + spread/(bar_bps)`, which lands 1.5-1.7 across
  every asset class. That's a suspiciously flat surface: crypto with 8-20bps spreads
  should demand a wider entry_z than FX at 1bp, and the current formula compresses them.
  Recompute against realized round-trip cost drag, not just spread nominal.
- `oversold` on RSI — the three-way 25/30/35 discretization is coarse. Fixed_income
  gets 35 across the board even though a short-duration bond and a long-duration bond
  have very different reversion timescales; either add a duration-scaled oversold or
  accept the coarseness and document.
- Confirmation-pattern filter — currently drops only `piercing_line` on 24/7 markets.
  Should probably also drop `bullish_engulfing` on crypto (its "gap-below-prior-close"
  criterion is a rounding artefact on continuous bars), and consider adding session-
  specific patterns for FX (Asian-session hammers behave differently from NY-session
  hammers). Requires per-symbol backtest evidence, not a-priori reasoning.

**Path forward:** run a sweep of Bollinger n_std ∈ {1.5, 2.0, 2.5, 3.0, 3.5} × asset-class
representative symbols with the walk-forward harness, scored on sortino_over_dd, and let
the surface pick the class-specific band width empirically. Same for ZScoreOU entry_z
∈ {0.5, 1.0, 1.5, 2.0, 2.5, 3.0}. Then fold the winners back into the calibration table
as class-specific constants replacing the current heuristic formulas. Bounded work
(~30 min per strategy × 4 asset classes) but needs the deep-history parquets already
built for crypto/FX + the equity cache.

## Audit gaps — 2026-09-04

Explore-agent audit sweep across the whole repository, ranked by severity, excluding items
already elsewhere in this document. Format: `severity file:line — one-liner`. Do NOT redesign
solutions; call out and prioritize. Full agent report lives in the session transcript.

### 🔴 Critical (act soon — live-path risk or silent correctness bugs)

- 🔴 `cli.py:483,1422` — `trading live` AND `autonomous_run` build `LiveMonitor` without
  `overlay_for`, `interpret_for`, `weight_bias_for`, `persistence_for`. The two CLI entry points
  that can touch real money bypass EVERY intel/overlay/allocator/persistence gate that the
  paper path uses. One-diff fix — copy the wiring block from `signal --paper` (cli.py ~189+).
- 🔴 `data/market.py:51` — `end = end or datetime.now(UTC)` is then part of the parquet cache
  key. `datetime.now()` changes every call, so `MarketData.history()`/`.recent()` NEVER hit
  the cache. Every `LiveMonitor.step()` symbol is a fresh live-broker fetch. Cache module
  exists but the primary caller can't reach it.
- 🔴 `data/market.py:22` vs `brokers/{kraken,ib,ib_web}.py` — interval-name mismatch. `MarketData`
  uses Questrade lexicon (`"HalfHour"`); Kraken uses `_INTERVAL_MINUTES` (numeric); IB uses
  `_INTERVAL_TO_BAR` (`"ThirtyMinutes"`). Non-QT 30-minute requests silently fall through to
  `OneDay`.
- 🔴 `monitor/live_loop.py:245` — `float(last.get("atr", price * 0.02)) or price * 0.02` returns
  `NaN` when `last["atr"]` is `NaN` (NaN is truthy). Strategy with NaN ATR silently sizes off
  NaN, corrupting stop distance and downstream risk math.
- 🔴 `monitor/live_loop.py:283-290` — `conviction *= weight_bias` is passed to `PositionSizer.size`
  which clips conviction to `[0, 1]`. All `weight_bias > 1.0` boost is discarded. The
  correlation-aware allocator's up-side amplification is silently no-op; only trims work.
- 🔴 `risk/kill_switch.py:67-80` — `KillSwitch.evaluate()` is defined + would auto-trip on
  drawdown/daily-loss, but NO CALLER anywhere invokes it. The drawdown-based auto-halt shown
  in the README architecture diagram (§#4) is manual-only.
- 🔴 `brokers/ib_web.py:509-511` — `place_order` auto-confirms IB warning prompts in a while
  loop with NO CAP. In live mode this can accept margin/exchange warnings a human should
  approve; also potentially loops unbounded on a misbehaving API.
- 🔴 `README.md:141` — Quick-start block includes `EXECUTION_MODE=live uv run trading live …`.
  `CLAUDE.md:24` explicitly says "Never set `EXECUTION_MODE=live` in code, tests, or example
  snippets." README example directly contradicts the CLAUDE.md non-negotiable.
- 🔴 `cli.py:1381-1383` — `os.environ["QUESTRADE_ENV"] = chosen_account` is written AFTER
  `get_settings()` has been called and after `_make_questrade(settings)` used the loaded
  settings. Runtime override is a no-op — users flipping `AUTONOMOUS_ACCOUNT=live` still hit
  whatever the frozen settings resolved.
- 🔴 `config/settings.py:210` — `@lru_cache(maxsize=1)` on `get_settings()`. Any test/process
  that mutates env/yaml AFTER first call gets stale settings. Compounds with the autonomous_run
  env-mutation bug above.

### 🟡 Medium (correctness / hygiene)

- 🟡 `brokers/kraken.py:382-387` — `_txid_to_int` uses `hash(txid)` which is randomized per
  Python process (PYTHONHASHSEED). Docstring calls it "stable" — it is not. Kraken fill row
  `order_id` changes across restarts; joining fills back to orders breaks post-restart.
- 🟡 `execution/daily_budget.py:54-88` — `snapshot()` re-parses the entire `state/orders.jsonl`
  from disk on every gate check. Scales linearly with journal size; runs synchronously inside
  `Router._gate` on every intent.
- 🟡 `data/cache.py:40-45` — `put()` writes parquet directly with no atomic rename. Crash
  mid-write leaves a corrupt file; subsequent `get()` returns `None` but the corrupted file
  stays forever (never overwritten because of the time-varying key bug).
- 🟡 `brokers/ib.py:354` — `Stock(order.symbol, "SMART", "USD")` hardcoded. Non-US equities
  (e.g. `XIC.TO`) become the wrong contract; futures/bonds registered via `IBContract` elsewhere
  are not consulted by `place_order`. Silent mis-routing on the socket path.
- 🟡 `monitor/live_loop.py:398` — `heat = existing_risk / equity`, but `existing_risk` is DOLLAR
  CVaR/ATR risk while `hedge_weight` expects a fraction. Units mismatch when the hedge
  overlay is enabled.
- 🟡 `brokers/models.py:161` — `Fill.venue: Literal["paper", "questrade-practice",
  "questrade-live"]`. Kraken/IB/IB-Web fills MUST be constructed with `venue="paper"` (see
  `paper.py:165`); any adapter that writes `Fill` directly with `venue="kraken"` fails Pydantic
  validation. Schema anchored to pre-multi-venue world.
- 🟡 `execution/asset_router.py:23-37` — `DEFAULT_ASSET_BROKERAGE`/`ASSET_LEAN_SPEC` still say
  `InteractiveBrokersBrokerage`, but the runtime brokers implemented are `IBBroker`,
  `IBWebBroker`, `KrakenBroker`, `QuestradeBroker`. AssetRouter is LEAN-oriented and doesn't
  route to any real Broker adapters.
- 🟡 `execution/asset_router.py:16` — `AssetClass = Literal["equity","future","commodity",
  "crypto"]`. But `intel/overlay.py::OverlayClass` now carries `fixed_income`, `precious_metals`
  (2026-09-03 close of gap #4). Two class enums have drifted apart.
- 🟡 `intel/apply.py::apply_overlay` — Grep shows only the `intel/__init__` export and its test
  import it; no runtime code path. Public API surface that's effectively dead.
- 🟡 `brokers/paper.py:186-220` — `mark_to_market` swallows quote failures per symbol. If the
  feed is throttled/down, equity CSV keeps writing STALE unrealized P&L with no telemetry that
  the marks are stale — the max-drawdown kill-switch invariant would read a lying number.
- 🟡 `brokers/base.py:22-48` — `Broker` `Protocol` does not declare `venue: str`, but every
  concrete broker declares it and `PaperBroker.__init__` depends on it. A new adapter that
  omits `venue` type-checks fine but silently falls back to `.name` at `paper.py:64`.
- 🟡 `cli.py:496-497` — `trading live` command WARNs but does NOT abort when
  `QUESTRADE_ENV != "live"`. User typing the phrase can run "live" mode against the practice
  environment thinking it's live (or vice versa).
- 🟡 `data/cache.py:22-28` — Path uses first 16 chars of SHA-256 hex, no schema version in the
  filename. Future OHLCV column change can't invalidate existing parquets.
- 🟡 `data/market.py` — no gap-filling / incremental append. Every `history()` call re-fetches
  entire window rather than fetching only missing tail.
- 🟡 `brokers/ib_web.py:107` and `config/settings.py:73` — `verify_ssl=False` default (CP Gateway
  self-signed cert). If a caller mis-points `ib_web_host` off `localhost`, TLS validation is
  silently skipped. No guard that the target IS localhost.
- 🟡 `monitor/alerter.py:43-60` — `_telegram` no rate-limit (30-msg/sec Telegram cap will 429),
  no response-status check, no scrub for anything containing secrets (fine today but no
  guardrails).
- 🟡 Test coverage — no test files for `data/market.py`, `data/cache.py`, `execution/journal.py`,
  new futures pipeline (`data/futures_calendar.py`, `data/futures_continuous.py`,
  `data/ib_futures_history.py`), `microstructure/{bitstamp_l2,coinbase_l2,simulator,arbitrage}.py`,
  `intel/{apply,chart,worldmonitor}.py`, `scripts/liquidity_heatmap.py`,
  `scripts/walk_forward_pairs.py`, `scripts/sweep_ib.py`.

### 🟢 Minor (docs / cleanup / future risk)

- 🟢 `README.md:186-206` — "What ships in the box" table lists 6 strategies but
  `strategies/examples/` actually holds 11 files (also arima_garch, mean_reversion, momentum,
  seasonality, volatility). Stale.
- 🟢 `README.md:232-234` vs `CLAUDE.md:118-120` — Two docs disagree on the agent set. README
  lists 2, CLAUDE.md lists 3 (adds `autonomous-monitor`).
- 🟢 `CLAUDE.md:60-64` — "Entry points" section is stale relative to actual layout — doesn't
  mention `intel/`, `portfolio/`, `microstructure/`, `models/` or the four broker adapters.
- 🟢 `brokers/paper.py:47` — `_order_counter: Iterator[int] = itertools.count(1)` is a CLASS
  variable. Two PaperBroker instances in the same process share it. Fine today; will bite
  two-broker research scripts.
- 🟢 `brokers/token_store.py:39` — Fixed salt `b"trading-live-claude/v1/tokens"`. If two users
  share the same `TOKEN_ENCRYPTION_KEY`, they get the same Fernet key. Per-install random salt
  would be safer.
- 🟢 `pyproject.toml:9-26` — every runtime dep is `>=` with NO upper cap. Breaking major
  (pydantic v3, pandas v3) would install and break silently. No `uv.lock` review at commit time.
- 🟢 `pyproject.toml:31-33` — `ib_insync>=0.9.86` is optional but effectively unmaintained
  (last release 2023). `brokers/ib.py` depends on it. Long-term move to `ib-async` (community
  fork) is likely required.
- 🟢 `pyproject.toml` — `PyJWT` used in `ib_web.py::_mint_client_assertion` but NOT declared
  in any extras. Lazy import + `BrokerError` on ImportError catches it, but users of
  `OAuth2JWTAuth` get no install hint until first token exchange.
- 🟢 `brokers/questrade.py:16` — `LOGIN_HOST` hardcoded — no override for QT's practice sandbox.
  `questrade_env=practice` has no effect on this constant (the auth flow returns the api_server
  anyway).
- 🟢 `state/paper_fills.jsonl`, `paper_orders.jsonl`, `paper_equity.csv` — `session_id` added
  post-hoc; older rows don't have it. No migration script; analytics joining by session_id must
  tolerate NULL.
- 🟢 `execution/router.py:229-247` — router journals order intent BEFORE the kill-switch check;
  intent is written even for HALTED, then written again as rejected. Harmless duplication but
  grows journal on halted state.
- 🟢 `intel/graph.py:397-406` — `append_edges` catches all exceptions and logs; a full-disk
  condition silently drops graph write with nothing surfacing to the operator.
- 🟢 `monitor/alerter.py:71` — hardcodes SMTP port 465; no `AlertConfig.smtp_port` field.
  Users on 587/STARTTLS can't configure.
- 🟢 `brokers/paper.py:52` — `starting_equity=100_000.0` hardcoded across CLI callers; not
  read from `trading.yaml`.
- 🟢 `monitor/live_loop.py:38-40` — `_INTERPRET_BIAS_FLOOR = 0.25` and confidence factor map
  hardcoded; not configurable.

### Top-5 recommended fixes (agent's ranking) — status as of 2026-09-08

1. **Wire intel/overlay/allocator/persistence hooks into `trading live` and `autonomous_run`**
   🔴 STILL OPEN. Single-diff copy from `signal --paper` wiring block (cli.py ~189+).
2. ✅ **`MarketData` cache fix** — LANDED commit `638f8d3` (floor `end` to interval boundary).
3. ✅ **Interval-name mismatch across brokers** — LANDED commit `638f8d3` (IB/Kraken lexicon
   canonical; Questrade translates on its own side).
4. ✅ **Conviction-clip / weight-bias contradiction** — LANDED commit `638f8d3` (raised to
   [0, 3.0] matching `weight_bias` cap in live_loop).
5. ✅ **Wire `KillSwitch.evaluate` into `PaperBroker._journal_equity`** — LANDED 2026-09-08.
   PaperBroker._journal_equity now calls evaluate() with current equity + peak + day-open,
   trips the file sentinel on breach; day-open equity resets on UTC date change. Combined
   with the tightened 3.0% max_drawdown_kill_switch default, auto-halt in the README
   architecture is now real.

---


## 0. Full resweep — reproducible calibration on the expanded universe  🟢 SCRIPTED, run pending

Universe expansion + reproducible-calibration plumbing landed this session (commit `6d20e50`).
`scripts/sweep_universe.py` now takes `--min 0 --max 1_000_000 --wf-top 30 --tag <label>
--carry-held` (default ON) and writes to `reports/sweep_{tag}_{panel,walkforward}.csv`. Held
names (CGL.TO, ZUT.TO, SDE.TO) always reach the walk-forward stage regardless of screen filters,
flagged in the output — so a resweep can never silently drop coverage of what we actually own.

**Runbook to complete next session** — 3 steps, foreground shell (Windows stdout buffers in
background; ~476 cached names → ~439 pass screen → panel ~40 min, WF ~10 min more):

```
# 1. Pre-warm the cache for held names + the expanded SEED (adds anything Questrade will fetch).
#    Held names had NO cached history on the partial resweep run — this is the exact "silent
#    drop" the carry-in was designed to catch, and warm_cache.py is the fix.
python scripts/warm_cache.py --held --seed equity --years 5

# 2. Full resweep. Reports land at reports/sweep_resweep_full_{panel,walkforward}.csv.
python scripts/sweep_universe.py --tag resweep_full --min 0 --max 1000000 \
    --wf-top 30 --min-bars 900

# 3. Inspect the WF output; edit analysis/universe.py::WALK_FORWARD_VALIDATED with survivors.
#    Held names appearing BELOW top-N in that CSV are research prompts, not auto-sells — the
#    current holding is not in the sweep's best cohort.
```

Also useful during the vertex/edge iteration:

```
# Grow the intel graph off-cadence (default 15-min vendor cadence; --sleep tunable).
python scripts/graph_journal.py --iterations 20

# Read the current shape any time (writes reports/graph_profile.md).
python scripts/graph_profile.py
```

Partial resweep observations from this session (killed at panel 50/439):
- Cached universe: 476 names with ≥900 bars; 439 pass ADV≥$1M and price>$0
- Held assets (CGL.TO, ZUT.TO, SDE.TO) had NO cached history — warm_cache is the prereq
- Panel-stage throughput: ~10 names/min ⇒ ~40 min for the full 439, then ~30 WF folds ~10 min more

## 1. Cross-interface arbitrage — interlisted equities (TSX ⇄ NYSE)  ✅ BUILT
`microstructure/interlisted.py::InterlistedArb` — FX-adjusted TSX/NYSE dislocation detector, tested,
committed. Live scan of 25 pairs confirmed the honest verdict: 0/25 clear at retail FX (~180 bps),
~1/25 marginally at institutional (~3 bps). **Follow-up if revisited:** run it during **market
hours** (the scan ran at ~2am on wide/stale closing quotes), add a **stale-quote sanity filter**
(reject implied-FX deviations too large to be real — the MFC +500 bps artifact, the interlisted
VELO), and stream live quotes rather than one-shot.

## 2. Deeper crypto history → walk-forward the crypto sleeve  ✅ CODE + PIPELINE
`data/kraken_ohlc.py` now has `kraken_trades_paginated` / `aggregate_trades_to_daily` /
`kraken_ohlc_deep` (see commit `cee64b3`), and there are two scripts:
- `scripts/fetch_crypto_history.py` — pulls multi-year history for every sleeve pair via the
  paginated `/0/public/Trades` endpoint and caches parquet under `data/cache/`. Slow (Kraken
  public tier is ~1 req/s and pages hand back ~1000 trades); resumable via `--since`.
- `scripts/walk_forward_crypto.py` — reads the cached parquets and runs the same walk-forward
  helper the equity sweep uses (2y train / 6mo test, per-fold re-opt, WFE ≥ 0.5 ∧ OOS>0
  ∧ ≥10 trades). Reports to `reports/walk_forward_crypto.csv` and prints promotion candidates.

**Still to do:** actually run `fetch_crypto_history.py` (multi-hour on the network) and then
`walk_forward_crypto.py`, and edit `CRYPTO_SLEEVE` tiers based on the report. The script deliberately
does NOT auto-flip the tiers — that belongs on a human diff.

## 3. KrakenBroker — let the Router fill crypto orders  ✅ BUILT (commit dff46ff)
`brokers/kraken.py` implements the `Broker` protocol against Kraken's public + private REST APIs,
committed and registered in `brokers/__init__.py`, with respx-mocked tests. Live placement is
gated behind `enable_live_orders=True` at construction — the switch is per-instance and no code
in the repo flips it on its own. Used today by `scripts/paper_kraken.py` as the market-data feed
wrapped in `PaperBroker`.

**Still pending:** thread through `AssetRouter` for live crypto order routing when the go-live
decision is made. Not urgent — paper path is complete.

- Fractional sizing: `Order.totalQuantity` is `float` already; the router will need to skip its
  integer round on `.crypto` symbols. Small change in `execution/router.py`.
- Secrets (Kraken API key/secret) in `.env` only, same as Questrade; never commit them.

## 4. Richer interpret.py catalog — three new thesis motifs  ✅ BUILT (commit 1278d69)
Dollar strength divergence (primary + mirror), Disaster / insurance underpricing, Commodity
carry-inversion proxy (moderate-cap, awaits real futures-curve feed). New `insurance` and
`emerging_markets` theme keys, 7 focused tests. **Follow-up:** ingest a live futures-curve
feed so the carry-inversion thesis fires on the real signal rather than the stress+flow proxy.
_(Original spec pruned 2026-09-08 — recover from commit `1278d69` if needed.)_

## 5. Paper-trading journal upgrade  ✅ BUILT (commit 2173d35)
`PaperBroker` now emits `state/paper_orders.jsonl`, `state/paper_equity.csv`, and stamps a
per-instance `session_id` on every row across all three journals. Realized/unrealized P&L, peak
equity, and drawdown_pct feed the max-drawdown kill-switch invariant. 7 focused tests.
**Follow-up (queued):** tick-aware fill prices instead of full-precision floats — do this when
tick-aware sizing enters the router.
_(Original spec pruned 2026-09-08 — recover from commit `2173d35` if needed.)_

## 6. GraphRAG / multi-agent overlay on the intel wing  🟡 SHIPPED + HELD

**Current posture (decided this session): keep the pipeline informative on the rule + graph
layers alone; iterate vertices/edges from journaled inputs as the record accrues; hold the
specialist/adversary agent sim layer.** The agent layer is built and tested, but wiring it into
the live poll cadence needs an Anthropic Console API key (separate from Claude Desktop, ~cents
per debate run at Sonnet 5), and the value only compounds once the graph journal has meaningful
depth — running debate on a thin corpus is expensive noise. Revisit when the graph has weeks of
history and there is a specific hypothesis worth the LLM round-trip to sharpen.

**What's built and running today:**
- `intel/graph.py` — append-only edge journal at `state/intel_graph.jsonl`. Snapshot decomposes
  into typed `(subject, predicate, object, weight, ts)` rows. Node types: poll, domain, region,
  source, market, event. Predicates: observed, elevated_in, co_occurs, stressed_by, mentioned_by,
  about_domain, affects_region.
- **Per-event decomposition** (commit `75206bf`) — `WorldMonitorClient.snapshot` writes per-event
  edges from news cross-source signals, advisories, and conflict strategic-risk sample. Vendor id
  preferred; deterministic hash of title+timestamp fallback. Corroboration is now a graph query.
- `recent_events_from_graph()` projects edges back into evidence records for downstream consumers.
- `edge_persistence()` query — "this predicate→object edge has held for N consecutive polls".
- Inverse-weighted source freshness (commit `75206bf`) — market-driven gates (fear, VIX, DXY,
  crypto_vol) now discounted by the market payload age; the freshness formula weights each
  source by its own freshness so a stale source drags the blend down less than a naive mean.

**What's built but held (not wired into the live path):**
- `intel/agents.py` — SpecialistReader (per domain), Adversary, `debate()`. Real Anthropic
  Messages API calls (no injectable stub); respx-mocked in tests. Commits `c925916` + `f523349`.
- `interpret.py::enrich_with_agents()` — merges FiredThesis rows into the rule reads with
  vocabulary-aligned confidence bands. Deliberately explicit-opt-in; not called from anywhere on
  the deterministic hot path. Kept available so a future runner (e.g. `scripts/paper_intel_debate.py`)
  can call it on-demand once a Console key is in place and the graph journal is deep enough to
  reward the LLM cost.

**Iterative next steps that keep this posture:**
- Extend `snapshot_to_edges` and the vendor-payload decomposition as new evidence shapes surface
  (e.g. per-event severity edges, per-actor mentions, per-corridor edges for shipping/energy).
- Grow the `edge_persistence` query family: co-persistence across two edges, thickening rate,
  first-seen recency. These are the features `intel/history.py` cannot express on the flat frame.
- SQLite backend once the JSONL grows past a few MB — the query surface stays the same.

_(Original speculative spec — MiroFish/OASIS/GraphRAG design notes — pruned 2026-09-08. Full
text recoverable from git history if the agent-layer path is revisited.)_

## 7. Crypto WF — shallow fallback  ✅ CODE + PIPELINE + PROTOCOL VALIDATED (2026-09-08)
`scripts/walk_forward_crypto.py` shallow-fallback closed 2026-09-04 (uses
`kraken_ohlc(pair, interval=1440)` when deep-history parquet absent; tier=`screened+`
for shallow-derived rows, never `robust`). Protocol validated 2026-09-08 across all 13
CRYPTO_SLEEVE pairs (2 deep / 11 shallow). Tier promotions DEFERRED per data-first rule
— see the "Deep-history fetch (currencies sleeve)" queued item above. Report at
`reports/walk_forward_crypto.csv`.

## 8. Pair-trading strategy oriented to FX pairs

`strategies/examples/pairs.py` is cointegration-based and asset-agnostic — feed it two
co-integrated symbols and it emits entry/exit against the spread. Currently calibrated on
equity pairs (`RY.TO/BNS.TO` etc). To use it on FX:

**Blockers:**
1. **No FX price feed wired.** Questrade returns FX only inside its Cdn ADR / interlisted arb
   plumbing, not as standalone pair quotes. Kraken quotes `EUR/USD`, `USD/CAD`, etc. natively
   for the pairs it lists, and via `KrakenBroker.quotes` that's already accessible — enough for
   a small MVP.
2. **No FX-side cointegration research.** Equity pairs cointegrate on shared factors (bank
   fundamentals, sector cyclicality); FX pairs cointegrate on rate differentials, real-vs-
   nominal moves, and carry — different mean-reversion horizons and different appropriate
   half-lives.
3. **`WF_PROTOCOLS["fx"]` is registered** with the right shape (504/126/260-annualization) but
   `data_source="pair-price feed (not yet wired)"`. Same honest gap as futures.

**Realistic path:**
- **MVP:** enumerate the FX pairs Kraken lists; use `KrakenBroker.quotes` + `kraken_ohlc` to
  build daily histories; sweep `pairs.py` across every FX-pair combination that has cointegrated
  history (Engle-Granger + Johansen, pick the pair pool). Uses the fx protocol.
- **Later:** add `data/fx.py` alongside `data/kraken_ohlc.py` / `data/market.py` — an FX-vendor
  adapter (OANDA / Alpha Vantage / Polygon FX) with the canonical OHLCV shape. Then the FX pair
  universe widens beyond Kraken's fiat list.
- Register FX-specific parameter grids (shorter mean-reversion half-lives than equity pairs).

## 9. Cross-path wiring — intel ↔ trading ↔ alerter  🟡 TIERS 1-3 DONE

Status as of 2026-09-08:
- ✅ **Tier 1:** OSINT scalar + interpret filter + allocator bias + Alerter (both QT + Kraken)
- ✅ **Tier 2:** Fills → intel graph via `traded` predicate + `fill_edge()` helper
- ✅ **Tier 3:** Graph persistence → entry gate via `PersistenceGate` (11 focused tests)
- ⏳ **Tier 4:** Realized P&L → thesis calibration — **DATA-BLOCKED**, needs weeks of paper
  fills matched against active theses. 7 days accrued / ~30 days minimum. Revisit ~2026-Oct.
- ⏳ **Tier 5:** Prediction evaluation — **DATA-BLOCKED**, needs 21-day forward-return windows
  post-thesis fire. Current thesis history 10 days = zero 21-day windows. Revisit ~2026-Nov+.

_(Original spec pruned 2026-09-08 — the "missing wires" narrative was subsumed by the tier
list above. Recover from git history if needed.)_

## 10. Correlation-aware allocator on the crypto + equity sleeves  ✅ BUILT (commits 365452a + f9acdcc)

Landed on both venues:
- **Crypto:** `paper_kraken.py` computes bias from 720-day daily OHLC + screen_score, biases
  per-pair conviction via `LiveMonitor.weight_bias_for`. Current 2026-09-08 bias distribution:
  BTC/PAXG at 3.90× cap, ZEC 0.58× (real diversifier tier), cluster names 0.24-0.51×.
- **Equity:** `cli.py signal` computes bias from WF-validated OOS scores + 252-bar return
  histories from local cache. Symbols not in `WALK_FORWARD_VALIDATED` default to neutral 1.0×
  (as seen with RSI.TO / RIG.TO in the 2026-09-08 pool).

**Follow-up:** recompute correlation matrix on cadence (weekly?) and write snapshots to
`state/{crypto,equity}_corr.jsonl` for auditability. One-off correlation study for the 13-pair
sleeve landed as `reports/crypto_corr_2026-09-05.{csv,png}` — not yet on cron.
_(Original spec + correlation cluster table pruned 2026-09-08 — recover from git history.)_

---

## 11. TradeCard — six-axis gap closure  🟢 INTEGRATED, GAP CLOSURE IN PROGRESS

**Status (2026-09-08).** Landed on `feat/tradecard-approval` at
<https://github.com/akshan-bansal/FRM-Claude/pull/new/feat/tradecard-approval>
as 10 coherent commits (through the thesis-prose framing patch).
**44 tests pass.**

### Architectural decision — zero vendor infrastructure

Approved 2026-09-08 after weighing security auditors, backend cost, and
user updateability against each other. The full rationale is in the
memory file `tradecard-zero-vendor-infra-architecture.md`; the summary:

- **The vendor operates no recurring services.** Firmware ships via
  **GitHub Releases** (signed with sigstore/cosign in CI); PWA / docs /
  OpenAPI ship via **GitHub Pages**; push notifications ride a
  **user-chosen relay** (ntfy.sh topic or native APNs/FCM through a
  PWA-registered VAPID key on the user's own shim). No user accounts,
  no telemetry, no push tokens stored server-side.
- **The shim is single-owner-per-box.** Multi-tenant / per-owner bearer
  tokens are a coordinator concept and are dropped from the near-term
  plan. Each box is one tenant.
- **Recurring vendor cost stays $0/month** whether there are 10 users
  or 10,000 — the card is a one-time purchase and the software is
  open-source; nothing recurs.
- **Supply-chain concentration on GitHub** is mitigated by sigstore
  signatures + a public transparency log, matching the pattern npm /
  PyPI / Docker already ship.
- **Non-technical customers** get a bundled RPi-class box pre-flashed
  with the shim (manufacturing allies produce the hardware). The vendor
  still doesn't operate services — the box runs the same open-source
  shim, offline-capable.

The revised first-week migration plan (below) reflects this: no
`owners` table, no coordinator client, no vendor push service.

### Sub-obj 4 queue (next commits on the same branch)

- **URL versioning to `/v1/…`** — freeze the wire before firmware pins its paths.
  Every route the shim serves today gets remounted at `/v1/{route}`; the
  bare paths (e.g. `/intents/pending`) return `301 Moved Permanently` to
  `/v1/…` for one release, then drop. `info.version` in the OpenAPI spec
  goes to `1.0.0`. Card simulator + firmware README + the paper scripts'
  “register a card via POST /card/register” lines all get bumped. Add a
  CI check that fails when a live route lacks a `/v1/` prefix.
- **Multi-broker routing inside a single Router** — today's `Router` holds
  one `Broker` instance and the card just displays whatever
  `router.broker.name` is set to. Turn `intent.broker` into a real routing
  key: `Router(brokers: dict[str, Broker], default: str)`, dispatch inside
  `submit()` selects by `intent.broker` (falls back to `default`), and
  every existing risk gate keeps running unchanged. `wire_card_approval`
  passes the dispatched broker name into the prompt so the WYSIWYS
  canonical remains the true destination. Follow-ups this enables:
  IB-plus-Kraken on one paper process, per-broker daily budgets, and the
  Canadian-user story where equities go to IB and crypto to Kraken.

Both build cleanly on what's already merged; nothing here changes
`Router._gate` or the autonomous-daemon gate list.

### Shim runtime migration — first-week plan (revised for zero-vendor-infra)

Runs on `feat/tradecard-approval` in order:

1. **URL versioning to `/v1/…`** (as above).
2. **SQLite persistence for the store** — replace the in-memory
   `dict` + `deque` behind the existing `ApprovalStore` Protocol.
   Prompts survive restart, `passbook_max` becomes a `LIMIT`, indexes
   on `resolved_at` and `intent_id`. Single schema, single owner.
3. **~~Owners + per-owner bearer tokens~~** — DROPPED. The
   zero-vendor-infra decision makes the shim single-owner-per-box; a
   bearer token still exists but scopes to the box, not to a user
   record. Reconsider only if a household wants shared-box multi-card
   with per-card scopes — not a v1 concern.
4. **FastAPI port** — same endpoints, same OpenAPI spec (auto-generated
   from pydantic models lifted from `approval_schemas.py`), TLS via
   `uvicorn --ssl-keyfile`, native SSE / WebSocket for the push channel.
5. **Packaging** — `pip install trading-live-claude[shim]`, a Docker
   image on Docker Hub, systemd unit under `deploy/`.
6. **New: sigstore-signed release workflow** — GitHub Actions job that
   builds firmware images + wheels on tag, signs with cosign, publishes
   the signatures to the transparency log, attaches to the GitHub
   Release. The card's OTA path verifies these signatures on download.
7. **New: PWA scaffold** — static Svelte or preact build published to
   GitHub Pages under `/app`, discovers the LAN shim via mDNS or a
   user-typed URL, uses the OpenAPI spec to codegen the client.

Not on the near-term list: coordinator service, cloud-hosted anything,
plugin sandbox, Postgres, phone-as-shim. Those come only when there are
real users forcing the decisions.

**Sub-objective 0: complete integration into the GitHub repo — DO THIS FIRST.**
Everything else in this section presumes the code is landed on `main` behind a feature flag,
not sitting as an uncommitted diff on `feat/multi-scoring-attention-map`. Concrete steps:

1. **Branch off cleanly.** Create `feat/tradecard-approval` from the current branch's HEAD
   (or from `main` if the multi-scoring work has already merged). Rebase down to a series of
   coherent commits: (a) `approval.py` + tests; (b) `vs_engine.py` + tests; (c) shim +
   simulator + shim tests; (d) firmware skeleton; (e) paper-script `--require-card` wiring.
2. **Split `firmware/` from Python packaging.** Right now it sits at the repo root outside
   `src/`; add `firmware/` to `.gitignore` for the Python wheel and note in `pyproject.toml`
   that the wheel only ships Python. The ESP-IDF project is standalone.
3. **Fix the cross-package import in `wire_card_approval`.** It currently reaches into
   `scripts.approval_shim` via an injected `shim_starter`. Move the HTTP server into
   `src/trading_live_claude/execution/approval_server.py` so the module dependency arrow
   points the right way, and turn `scripts/approval_shim.py` into a thin CLI wrapper.
4. **Docs**: extend `CLAUDE.md` with a "TradeCard" section (feature flag, security posture,
   which scripts honor it, how to run the shim + simulator end-to-end without silicon).
5. **CI**: add `tests/test_approval_shim.py` to the default run; keep it out of the
   coverage-gate baseline if it flakes on socket-bind races on Windows CI.
6. **Changelog / release notes** entry — user-facing description of `--require-card` and the
   published brief link.
7. **Open a PR** with the published-brief artifact URL in the description so reviewers can
   see the shape without pulling the branch. Keep `execution_mode` and `AUTONOMOUS_ENABLED`
   untouched; card approval is orthogonal to live/autonomous.

Do NOT commit any of this until the user says so — the standing rule in
`memory/no-commits-without-explicit-ask.md` still applies.

### Sub-objective 1: hardware

- No schematic, no BOM, no PCB. `firmware/tradecard/main/main.c` documents a pinout but
  there is no board that wires it up.
- No secure element. Private Ed25519 key sits in NVS flash — trivially readable over UART
  with `esptool.py read_flash`. Migration target: ATECC608A (I²C) or the ESP32-S3 DS
  peripheral so the sk never leaves silicon.
- No power path. LiPo cell + charging IC (MCP73831 class) + fuel gauge (MAX17048) + boost
  converter not selected. Card cannot run untethered.
- No enclosure. Credit-card form factor is aspirational — the ESP32-S3-DevKitC-1 is roughly
  10× the volume. Realistic target for v0.3: business-card-thick 3D-printed shell with an
  ESP32-S3-MINI-1 module and an FPC-attached PCD8544.
- No display sourcing decision. Nokia 5110 modules on the aftermarket are aging; SSD1306
  128×64 OLED and Waveshare 2.9" e-paper haven't been evaluated as alternates.
- No physical five-key input array. Dome-switch vs tactile vs capacitive not chosen.
- No RF-certification path (FCC / IC / CE) for the Wi-Fi radio.
- No antenna decision (PCB antenna on ESP32-S3-WROOM-1 vs external chip antenna with a
  U.FL pigtail).

### Sub-objective 2: firmware

- `lcd_puts` is a UART mirror. Real 5×7 font + framebuffer painter (u8g2 or Adafruit-GFX
  port) not linked. Nothing appears on the physical LCD yet.
- No NTP sync. `handle_prompt` uses a hardcoded 60-second TTL rather than parsing
  `expires_at` against a synced clock. First real-world prompt with the wrong TTL will
  expire early or run past its actual deadline.
- No TLS. `esp_http_client` uses plain HTTP; the mbedTLS bundle is configured in
  `sdkconfig.defaults` but the client never asks for it. Card ↔ shim is in the clear.
- No deep sleep. Wi-Fi stays on between polls; battery budget for a card-form-factor cell
  measures in minutes, not hours.
- No CENTER-button detail view. `GET /intel/{ref}` shipped on the shim side; the firmware
  does not call it, so the thesis writeup can't be pulled up on the card.
- Trust-on-first-use pairing. Anyone with physical access can flash a new key and
  re-register; the shim has no way to distinguish a real ATECC608A-attested key from a
  spoofed one.
- No firmware OTA. Updates require USB re-flash.
- No factory-reset gesture (e.g. hold CENTER 10s to wipe NVS keys) — a compromised card
  cannot be rekeyed by the user.
- No fault UI. Wi-Fi drop, shim unreachable, signature-rejected responses aren't surfaced
  on the LCD.
- Passbook has no filter/search — only linear scroll.
- No long-press detection for mode switching (accept-vs-detail vs passbook-scroll gestures
  will collide once the CENTER view lands).

### Sub-objective 3: VS investment engine software

- Deterministic-rules only. No LLM path for a richer prose narrator when the caller wants
  one (opt-in via `thesis_fn` swap is easy; not built).
- Thesis carries no confidence or attribution. "geo-risk 78" is a scalar; the writeup
  doesn't cite which WorldMonitor edges / events fired to move it.
- No historical-comparison clause. "Last week the same thesis on XIU.TO hit stop" would
  meaningfully change the reader's calibration; the passbook has the data, the engine
  doesn't use it.
- No feedback loop. Accepted vs declined vs expired verdicts aren't fed back into future
  thesis phrasing or priority (a decline pattern for a given clause could down-weight it
  next time).
- Broker → asset-class mapping is 1:1 in `ASSET_CLASS_HINT`. Reality: IB trades equities,
  futures, options, bonds, FX. The single-slot mapping under-labels multi-asset intents.
- No writeup pruning. `state/intel_writeups/` grows unbounded.
- No multi-lingual output — a Canadian user might want FR/EN toggle.
- Existing strategies still don't emit the `score`/`rank`/`r_multiple` columns the engine
  is prepared to lift via `MarketContext.from_signal_row()`. Contract is documented in
  `strategies/base.py`; adoption is per-strategy work.

### Sub-objective 4: API / plugin endpoints

- **Shim has no auth.** Anyone on the LAN can POST an intent (which prompts the card) or
  POST a fresh `/card/register` (adding a signer). Minimum acceptable: shared bearer +
  loopback-only; production: mTLS with a shim-issued client cert per card.
- No `DELETE /card/{card_id}` — a compromised card cannot be revoked without editing
  in-process state or restarting the shim.
- No `GET /passbook` — the shim knows every verdict but doesn't expose them for a
  companion app or web dashboard.
- No SSE / WebSocket push. Card and any web client both long-poll.
- No admin endpoint for listing active cards, pending intents, or writeup counts.
- No rate limiting on any endpoint.
- No CORS controls — a rogue web page loaded in the user's browser could POST to
  `localhost:8787` if that origin is ever reachable.
- No OpenAPI spec. Third-party integrations reverse-engineer from `approval_shim.py`.
- No URL / Accept-header versioning. First protocol change breaks every deployed card.
- Router still holds one broker at a time. Multi-broker routing (`intent.broker` picks
  the destination brokerage inside a single Router) is not implemented — the card just
  displays whatever `router.broker.name` is set to for this process.

### Sub-objective 5: user interaction

- No first-boot onboarding on the card. LCD shows nothing meaningful until the first
  prompt arrives.
- No queue-preview screen — user can't see "3 prompts pending" while browsing the passbook.
- No secondary "why" screen. The CENTER button is unimplemented, so the reader cannot pull
  up the full VS-engine thesis before deciding.
- No haptic feedback. User must look at the card to know a prompt arrived.
- No LED / bezel indicator for a pending prompt.
- No per-user profile or PIN before signing — card is single-tenant.
- No timeout-warning UI. TTL just runs down silently.
- Passbook has no "jump to today" or symbol filter.
- No language selection.
- Font size fixed; no accessibility affordance for low-vision users.

### Sub-objective 6: connectivity

- Wi-Fi only. No BLE (which was in the original blueprint for phone-tethered operation)
  and no cellular (NB-IoT). If home Wi-Fi drops, the card is inert.
- No connection-status indicator on-device. A dead shim looks identical to "quiet market".
- No offline queue. A signed response with no shim reachable is lost — user's ACCEPT tap
  never lands.
- No on-device network configuration. SSID/PSK are baked at build time via menuconfig; a
  new Wi-Fi means a re-flash.
- No mDNS / auto-discovery for the shim URL.
- No captive-portal handling (hotel Wi-Fi).
- No handoff. Moving the shim to a new machine means re-flashing the card.
- No health telemetry from card → shim (battery, RSSI, last-seen), so the shim can't say
  "your card is offline" in a UI or an alert.
- Shim binds to loopback only. Documented options for exposing it beyond the same host
  (Tailscale, Cloudflare Tunnel) exist as prose but no scripted path.

### Sequencing (what unblocks what)

1. **Sub-objective 0** — landing the code on `main` behind a flag — is a hard prerequisite
   for everything else. Nothing else should be built on an uncommitted skeleton.
2. **Firmware font + NTP** (sub-obj 2) turns the LCD from a UART mirror into a real card
   surface — this is what makes hardware bring-up worth doing.
3. **Shim auth + `/card/{id}` revoke** (sub-obj 4) is the smallest thing that lets a card
   run outside a fully-trusted LAN.
4. **Hardware SE integration** (sub-obj 1) is only worth it once the key handling in
   firmware is written to feed off an I²C signer rather than an in-memory buffer.
5. **Multi-broker routing + strategy MarketContext adoption** (sub-obj 3, 4) is a good
   fit for the same PR since both hinge on the strategy signal-row contract.
6. Everything under sub-obj 5 / 6 is polish that lands after the card is a real physical
   object; UX for a virtual card is close to write-only.

**Published brief for this build (link stays live across sessions):**
<https://claude.ai/code/artifact/9567c2ac-b2bd-4797-adf5-2edbce2a4d90>
