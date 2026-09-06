# Next-session backlog

**Status (2026-09-02): ACTIVELY RUNNING.** Four parallel processes tick against paper venues +
intel graph; all commits on `feat/multi-scoring-attention-map`. Standing constraints unchanged:
new research clears the **walk-forward gate** before being tagged validated; live orders stay
behind the **human go-live confirmation**; use `httpx` (not `requests`); `ruff` + `mypy --strict`
+ `pytest` must stay green.

## Live processes (as of 2026-09-02)

1. **QT paper monitor** — `signal --paper --intel-overlay --level` over 14 names. Wired:
   OSINT overlay class scalar × interpret-thesis entry filter × correlation-aware allocator
   bias × strategy-vol gate × per-poll MTM. Rich Telegram alerts.
2. **Kraken paper monitor** — `paper_kraken.py` over 7 sleeve pairs. Now equivalently wired:
   OSINT overlay (crypto class scalar) × interpret filter × allocator bias × per-poll MTM. Also
   contributes to `state/intel_graph.jsonl` on every poll now (previously silent on intel).
3. **Graph journal poller** — `graph_journal.py --iterations 96 --sleep 900 --wash-min-hours 72`.
   Persistence + wash + thesis alerts to Telegram; temporal gate every 72h.
4. **Dashboard refresh** — `dashboard.py --refresh 300`. Rewrites `reports/dashboard.html`
   every 5 min with 9 sections (health, scalars, theses, persistence, sessions, equity curves,
   allocator bias, WF pool with backfilled win rates, graph profile).

## Journals (current depth)

- `state/intel_graph.jsonl` — 3000+ edges (grew from 571 across today's polling). Per-event
  decomposition working: event / poll / source / region / market / domain nodes.
- `state/paper_equity.csv` — 27+ rows across 8+ tracked session_ids, refreshed per poll (MTM).
- `state/paper_fills.jsonl` / `paper_orders.jsonl` — dozens of fills across restarts.
- `state/intel_overlay.jsonl` — flat overlay per poll.
- Real QT + Kraken accounts untouched (paper-only path throughout).

## Recent shipments (post-2026-09-01)

- Resweep survivors PROMOTED: `ENB.TO`, `XIU.TO`, `VDY.TO`, `SLF.TO` all in `WALK_FORWARD_
  VALIDATED` at tier=robust (commit `c3a0d18`). Pool now 25 robust + 7 watch.
- `CGL.TO` + `DBA` reclassified equity → commodity (metadata fix, same commit).
- `WF_PROTOCOLS` per-class registry (equity/commodity/crypto/future/fx) — `sweep_universe.py`
  reads window sizing from it (commit `75e3e6a`).
- OOS win rates BACKFILLED on 27 of 32 validated names (commit `d4a7022`). Skipped 5 with <5
  trades in current window (VFV.TO, ZEB.TO, ZWB.TO, QQQ, DBC).
- Notification formatter with WF evidence + sizing chain + win rate rendering (commit `78576aa`).
- Telegram plain-text mode (was returning 400 on Markdown-parsed rich alerts).
- Cross-path wiring item 9 tiers 1 (interpret → LiveMonitor), plus allocator bias, plus MTM.
- Kraken paper now gets OSINT overlay + interpret filter (gap #1 closed, commit `f9acdcc`).
- QT paper now gets correlation-aware allocator bias (gap #2 closed, same commit).
- Static HTML dashboard (`scripts/dashboard.py`, commit `ef28ef1`) — 9 sections, plotly.
- Live MTM on PaperBroker (commit `fce41a3`) — equity CSV now reflects mid-market per poll.
- Rename `thicken_graph.py` → `graph_journal.py` (commit `3dc11fe`).

## Runbook — resume from cold

```
# 1. Full test suite
python -m pytest tests/ -q --no-cov --ignore=tests/test_quantconnect.py

# 2. Refresh cache if stale (skips already-cached names)
python scripts/warm_cache.py --held --seed equity --years 5

# 3. Paper monitors (relaunch)
python -m trading_live_claude.cli signal --strategy bollinger \
    --symbols "EQB.TO,QQQ,XIC.TO,ZEB.TO,CGL.TO,VALE,ARX.TO,DBC,SRU.UN.TO,CRT.UN.TO,ENB.TO,XIU.TO,VDY.TO,SLF.TO" \
    --strategy-map "EQB.TO=ts_momentum,QQQ=ts_momentum,XIC.TO=rsi_meanrevert,ZEB.TO=atr_channel,CGL.TO=atr_channel,VALE=bollinger,ARX.TO=rsi_meanrevert,DBC=bollinger,SRU.UN.TO=rsi_meanrevert,CRT.UN.TO=rsi_meanrevert,ENB.TO=bollinger,XIU.TO=bollinger,VDY.TO=ts_momentum,SLF.TO=bollinger" \
    --interval 300 --paper --paper-equity 100000 --level --intel-overlay
python scripts/paper_kraken.py --interval 300 --paper-equity 100000

# 4. Graph journal + dashboard
python scripts/graph_journal.py --iterations 96 --sleep 900 --wash-min-hours 72 --persistence-threshold 5
python scripts/dashboard.py --refresh 300
```

## What's queued and unfixed

**IB-paper introduction (2026-09-03) opened four new gaps of its own.** Fixes #3 and #5 landed
in-session; #1, #2, #4, #6 are here.

- **Cross-path tiers 4 + 5** — Realized P&L → thesis calibration; prediction evaluation. Need
  weeks of accrued data before they can be built honestly.
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

### Top-5 recommended fixes (agent's ranking)

1. **Wire intel/overlay/allocator/persistence hooks into `trading live` and `autonomous_run`**
   — single highest-severity gap. One-diff copy from the `signal` command's wiring block
   (cli.py ~189+).
2. **Fix `MarketData` cache** — normalize `end` to a day/hour boundary before hashing, OR drop
   `end` from the cache key and store a `[start_ts, last_bar_ts]` metadata sidecar with
   incremental append.
3. **Fix interval-name mismatch across brokers** — either standardize on Kraken/IB's
   `"ThirtyMinutes"` and rewrite `_QT_INTERVAL_MAP`, or add broker-side translation. Silent
   fallback-to-daily for 30m requests across three brokers is a live-path landmine.
4. **Fix conviction-clip / weight-bias contradiction** — either `PositionSizer` accepts
   unclipped conviction (remove `_clip01` from `_vol_scale` and ATR path), or the LiveMonitor
   docstring admits that `weight_bias > 1` is discarded.
5. **Wire `KillSwitch.evaluate` into `PaperBroker._journal_equity`** — the drawdown is already
   computed there; call `KillSwitch.evaluate` with those values on every write. Auto-halt in
   the README architecture becomes real, not manual-only.

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
`emerging_markets` theme keys, 7 focused tests. Follow-up: ingest a live futures-curve feed so
the carry-inversion thesis fires on the real signal rather than the stress+flow proxy.

Original spec kept below for context.


`intel/interpret.py` currently fires five thesis rules (complacency divergence, energy concentration,
conflict escalation, sentiment stretch, quiet-tape null). The gap is that all five read *within*
their own domain — none of them cross-check against the FX layer, the disaster domain, or the
futures curve, which is where the next tranche of divergences actually lives. Add three motifs, each
with its threshold set, evidence extraction, action framing, and themes, and each guarded by the
`min_baseline_events` + `max_acceleration` clip already in `intel/events.py`. Same contract as the
existing five: hypotheses only, `action` framed as posture/research, never an entry signal.

- **Dollar strength divergence.** Fires when DXY is strong-and-rising (e.g. `market.dxy_chg` above
  a positive band) *while* a commodity or EM proxy is *also* rising — the divergence, because a
  stronger USD usually pressures USD-denominated commodities and EM assets. Also fires the mirror
  case: DXY weakening with commodities not rallying, which points at demand rather than currency.
  Inputs: `market.dxy`, `market.dxy_chg`, `market.crypto`, VALE / commodity ETFs from the pool.
  Themes: `dollar`, `materials`, `safe_haven`. Confidence upgrades when the divergence has been
  standing for multiple polls (use `intel/history.py::persistence`), not just this snapshot.

- **Disaster / insurance underpricing.** Fires when `natural_disasters_active` is elevated *and*
  the disaster domain's event acceleration is above baseline *while* the insurance-sector implied
  vol proxy (or, absent that, the broad market VIX) is not. Same complacency shape as the existing
  complacency-divergence thesis but on a different pair of independent inputs — disasters and
  reinsurance are one of the cleanest "physical world vs. market pricing" contrasts. New theme key
  `insurance` (reinsurers, catastrophe-exposed utilities) and reuse `materials` for the
  reconstruction-materials angle. `defense_geopolitical` does *not* apply here; keep the mapping
  narrow so implicated_symbols() stays useful for research seeding.

- **Commodity carry inversion.** Fires on a shift in the near-vs-deferred futures curve — the
  cleanest signal that supply stress has moved from headline to physical pricing. We do not have a
  live futures-curve feed in this project yet, so the first cut proxies it: `USO` (front-month oil
  ETF) vs. a longer-dated oil ETF's price ratio, tracked against `energy_stress` and
  `event_acceleration["energy"]`. Fires when the ratio has flipped direction (contango ↔
  backwardation proxy) with the energy domain running hot. Longer-term the honest fix is a real
  futures-curve fetcher (own dependency, own gitignored cache); flag that as a follow-up rather
  than blocking on it. Themes: `energy`, `materials`.

For each: add the rule to `interpret()` in strongest-evidence-first order (probably: dollar between
existing (1) and (2), disaster between (3) and (4), carry inversion at the end before the null),
add its exemplar tickers to `THEME_EXEMPLARS` (introduce the `insurance` key), and add one focused
test per rule covering fire / no-fire / evidence-shape. Update `test_intel_interpret.py`'s existing
theses-count assertions accordingly.

## 5. Paper-trading journal upgrade  ✅ BUILT (commit 2173d35)
`PaperBroker` now emits `state/paper_orders.jsonl`, `state/paper_equity.csv`, and stamps a
per-instance `session_id` on every row across all three journals. Realized/unrealized P&L, peak
equity, and drawdown_pct feed the max-drawdown kill-switch invariant. 7 focused tests.
Follow-up (queued): tick-aware fill prices instead of full-precision floats — do this when
tick-aware sizing enters the router.

Original spec kept below for context.

### (superseded spec)

`signal --paper` (committed 05e9b29) now routes intents through `PaperBroker`, and the first two
runs on 2026-08-31 produced four fills into `state/paper_fills.jsonl`. The wiring works and the
real account stays untouched, but the journal is too thin for the "one full week of paper trading
first" check that `CLAUDE.md` and the `live-trade-confirm` skill both require. Three gaps to close,
each small on its own:

- **`state/paper_orders.jsonl`.** PaperBroker currently records executed fills only, so a poll where
  the strategy fired but the broker declined to fill (insufficient equity, size == 0, stale quote)
  is silently invisible. Emit one row per intent that reaches PaperBroker, mirroring the shape of
  the router's `state/orders.jsonl` (mode, strategy, symbol, action, shares, entry, ts,
  accepted, rejected_reasons) so a run's intent-to-fill funnel is reconstructable.

- **`state/paper_equity.csv`.** Nothing snapshots equity — there is no P&L curve, no drawdown
  series, and nothing the go-live checklist can point at. Append one row per fill (and optionally
  a periodic mark-to-market snapshot on a fixed interval, gated by whether the market is open) with
  columns `ts, session_id, equity, cash, positions_value, realized_pnl, unrealized_pnl,
  peak_equity, drawdown_pct`. `peak_equity` and `drawdown_pct` are what feed the max-drawdown
  kill-switch invariant and are worth carrying in the row rather than recomputing on read.

- **`session_id` in every row.** Run 2 today stacked positions onto Run 1's in the same journal
  (204 sh EQB.TO + 86 sh QQQ combined across the two runs) because both started fresh at $100k
  with no session marker. Generate a session id at PaperBroker construction (uuid4 hex is fine)
  and thread it through every row written by that instance — `paper_fills.jsonl`,
  `paper_orders.jsonl`, `paper_equity.csv`. Enables per-session P&L, per-session drawdown, and
  the ability to reason about the paper record without accidentally averaging over overlapping
  runs.

Follow-up (separate, later): fill prices are currently full-precision floats (e.g. 715.5425925 on
QQQ) rather than snapped to the real tick size. Fine for P&L math, unrealistic for slippage
accounting. Address when tick-aware sizing enters the router; not urgent for the go-live check.

None of this touches the router or the risk gates; it is purely broker-side journaling. Once these
three land, one continuous paper session run through market hours for a week is enough to satisfy
the go-live pre-check, and `live-trade-confirm` can grow a real assertion against the equity CSV
(peak, drawdown, trade count) rather than the existence-check it can do today.

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

Original speculative spec kept below.

### (original speculative spec)

Larger and more speculative than items 4–5, kept here so the mapping doesn't get lost. Prompted by
reading [MiroFish](https://github.com/666ghj/MiroFish) — a multi-agent prediction engine layered on
[OASIS](https://github.com/camel-ai/oasis), CAMEL-AI's up-to-1M-agent social simulator. MiroFish's
three added layers (GraphRAG for grounding, Zep for long-term memory, a "Report Agent" that
synthesizes emergent behavior) map with unusual cleanness onto the intel wing's current gaps:

- **GraphRAG over the OSINT feed → aging the journal.** Replace flat `state/intel_overlay.jsonl`
  with a queryable entity/edge journal — nodes for events, actors, regions, commodities, sources;
  edges annotated with the snapshot timestamp and the source that asserted them. Persistence and
  corroboration then become graph queries ("this actor→region edge has been thickening across polls",
  "in-degree from independent source nodes") rather than per-field counters. This is the direct
  fix for the sparse-baseline / single-wire-inflates-a-feed artifacts that motivated the
  `min_baseline_events` and `max_acceleration` clips in `intel/events.py`.

- **OASIS-style specialist agents grounded in that graph → what interpret.py's rules should be.**
  Domain readers (energy, macro, geopolitics, disaster) each pull their slice from independent
  source sets and cast structured claims back into the same graph. Second-order chains a
  Suez-closure → European gas → EU fertilizer producers → North-American ag substitution — become
  derivable from the graph rather than needing to be enumerated by hand. Agents read the graph, not
  the raw payload.

- **Report Agent → what `intel/interpret.py` aspires to become.** Hand-picked thresholds
  (`VIX < 18`, `fg ≥ 60`, `energy_accel ≥ 2`) replaced by an agent that reports which motifs
  actually surfaced this poll, ideally with an adversary agent that tries to falsify each thesis
  against the same graph so only survivors fire. Same contract as today: hypotheses only, `action`
  framed as posture/research, never an entry signal.

**Concrete first step** — do NOT adopt MiroFish wholesale on the live path. The cheap, high-value
step is the layer MiroFish itself pivots on: replace `state/intel_overlay.jsonl` with an
append-only edge log (event, source, actor, region, timestamp) and rewrite `intel/history.py`'s
`change` / `relative position` / `persistence` queries on top of it. That gives ~80% of the
interpretive lift, keeps the live loop deterministic, and leaves OASIS-style simulation and agent
debate as an off-cadence enrichment layer whose outputs the live loop just reads. Backend choice
for the edge log: SQLite with a graph-shaped schema is enough to start; a real graph store (Neo4j,
as the offline fork uses) is a later question.

**Honest caveats before this goes past the sketch stage.**
- MiroFish is young (Dec 2025 release, hit GitHub trending March 2026, ~17k stars in a few months,
  undergrad-authored). Adoption is unproven; expect API breakage.
- Zep Cloud is a paid dependency. The English fork
  [MiroFish-Offline](https://github.com/nikmcfly/MiroFish-Offline) replaces Zep with Neo4j + Ollama;
  that's the branch to look at first for anything close to the live path.
- Does NOT solve the point-in-time-history problem. LLM-derived features are still forward-only
  until the graph journal accrues history. This is a quality upgrade to the live edge, not a way
  to backtest OSINT.
- Live-edge budget. A million-agent sim per poll does not fit in the poll cadence — the writer
  runs live, the simulation/debate layer runs off-cadence and caches theses.
- Confabulation risk. An LLM-invented edge in the corroboration graph is worse than no edge —
  it silently inflates the confidence axis. Hard rule: every edge is source-attributed, and an
  agent that cannot cite refuses.
- Adversarial inputs. Public news is half attackers. Reader agents run with strict output
  schemas, not free-form.
- The non-negotiables still apply. Nothing here sizes a trade. Every thesis remains a hypothesis
  and any name traded on it still clears walk-forward.

## 7. Crypto WF workaround — run against Kraken's shallow OHLC (no deep-history fetch required)

The crypto protocol landed this session (commit `c3a0d18`, `WF_PROTOCOLS["crypto"]`) is sized
for exactly this: `train=365 / test=91 / step=91`. Kraken's `/public/OHLC` caps at ~720 daily
bars, and `(720 − 365) ÷ 91 ≈ 3.9` folds — the honest minimum for a WFE calculation. Thinner
than the 12-fold equity WF but real out-of-sample scoring with real cost accounting.

**Concrete build (small, ~30 lines):**
- Extend `scripts/walk_forward_crypto.py` to fall back to `data/kraken_ohlc.py::kraken_ohlc`
  (shallow, one call) when the deep-history parquet does NOT exist. The `walk_forward` helper
  already reads the crypto protocol via the wiring in commit `75e3e6a`.
- Drop `MIN_BARS` from 900 to ~500 for the crypto sleeve only so 720-bar shallow data qualifies.
- Report label: pairs cleared on shallow WF are "screened+" — better than pure in-sample,
  thinner than deep-history WF. Not `robust` until the deep-history fetch clears them.

This unlocks a promotion path for the seven `CRYPTO_SLEEVE` pairs today, without waiting for the
multi-hour `scripts/fetch_crypto_history.py` run in item 2. Doesn't replace item 2 — deep-history
WF still supersedes shallow WF once the fetch has run.

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

## 9. Cross-path wiring — intel ↔ trading ↔ alerter as one recurrent-learning loop  🟡 TIER 1 DONE

Status as of 2026-09-02:
- ✅ OSINT scalar → sizing (both QT + Kraken)
- ✅ Interpret → entry-filter conviction bias (both QT + Kraken)
- ✅ Allocator → conviction bias (both QT + Kraken)
- ✅ Alerter as notification bridge (all events → Telegram)
- ⏳ Fills → intel graph event nodes (tier 2 — small)
- ⏳ Graph persistence → entry gate (tier 3 — needs paper validation)
- ⏳ Realized P&L → thesis calibration (tier 4 — needs weeks of data)
- ⏳ Prediction evaluation (tier 5 — needs weeks of data)

Original spec kept below.

### (original spec)

The pieces are all in place, they just don't talk to each other end-to-end yet. This item names
the interconnections so future work stays cross-functional / parallel / additive rather than
each track ending at its own journal file.

**Current state (already wired):**
- `OverlayProvider` — polls WorldMonitor, computes per-class scalars, journals to both
  `state/intel_overlay.jsonl` (flat) and `state/intel_graph.jsonl` (edges via
  `snapshot_to_edges` + per-event via `worldmonitor._write_event_edges`).
- Live trading path (`signal --intel-overlay`) uses `OverlayProvider` for size scaling.
- `intel/interpret.py::interpret()` reads snapshots into named theses (rule layer).
- `scripts/graph_journal.py` now fires persistence-hit + wash-event alerts to Telegram + email
  + stdout via the trading-path `Alerter` (commit this session).

**Missing wires (the actual cross-functional work):**

- **Interpret → LiveMonitor.** `interpret()` produces named theses per poll but nothing on the
  trading path reads them. Hook: LiveMonitor consumes the latest thesis list via a callable
  passed at construction (parallel to how `overlay_for` is wired), and uses the themes ↔
  exemplars mapping to bias which symbols get monitored, or to gate new entries in themes with
  ADVERSE claims. Small change, big meaning — turns rule-based reasoning into an entry filter
  (never an entry signal on its own — hypotheses only).

- **Graph persistence → entry gate.** `edge_persistence(edges, predicate="elevated_in",
  object=("domain", X))` already exists. Wire it into the risk gate: an entry in a symbol
  whose overlay class implicates domain X only fires if X's persistence is ≥ N. That's how
  "the same 6× event acceleration seen once is noise, the same reading across five polls is a
  regime" moves from a docstring into an enforceable check.

- **Fills → intel graph.** Every fill is a real point-in-time observation about the market's
  own state. Write fill rows into the graph as `event` nodes with a `filled_at` predicate.
  Then persistence queries can see "we've been entering this name for three polls" — same
  machinery, unified vocabulary.

- **Realized P&L → thesis calibration (recurrent-learning loop).** After N days, correlate
  which theses were live around each fill and how those fills subsequently performed. Store
  the correlations in `state/intel_thesis_pnl.jsonl` (a new journal). Feed the resulting
  hit-rate back into interpret.py's thresholds — the hand-picked constants (VIX<18, fg≥60)
  become adjusted-against-realized-outcomes over time. This is the "recurrent" part.

- **Prediction evaluation.** Each thesis fired at time T carries an implicated-ticker set. At
  T+N (N=5, 21 daily bars), measure whether those tickers moved as inferred. Journal the
  results. Publish as `reports/thesis_prediction.md` alongside the existing paper_report.md
  cadence. Turns interpret.py from "posture only" into a self-scored predictor — same
  hypothesis discipline (still not entry signals), plus a measurable track record.

- **Alerter as the notification bridge.** Every cross-path event should land in the same
  channel: overlay-halt, thesis fire, persistence hit (now wired), fill, drawdown threshold
  crossing, wash summary (now wired), degraded-feed warning. One channel, tagged by kind, so
  a phone reader sees the whole system's health at once.

**Sequencing that keeps the build additive and safe:**
1. Interpret → LiveMonitor read (advisory only, no gate change) — smallest change
2. Fill → graph write (data plumbing, safe by construction)
3. Graph persistence → entry gate (introduces a new gate, needs a paper session to validate)
4. Realized-P&L → thesis calibration (needs weeks of accrued data first)
5. Prediction evaluation (needs weeks of accrued data first)

Each step is testable in isolation and adds one hook to a boundary already established. None
require rewriting a hot-path module. All are gate-preserving — a broken cross-path never causes
a trade to fire without the risk gates that already exist.

## 10. Correlation-aware allocator on the crypto sleeve  ✅ BUILT (commit 365452a + f9acdcc)

Landed on both venues:
- Crypto: `paper_kraken.py` computes bias from 720-day daily OHLC + screen_score, biases per-pair
  conviction via `LiveMonitor.weight_bias_for`. Current bias: BTC/PAXG capped at 2.10x, ETH cut
  to 0.17x.
- Equity: `cli.py signal` computes bias from the WF-validated OOS scores + 252-bar return
  histories from the local cache. Symbols not in `WALK_FORWARD_VALIDATED` default to neutral 1.0.

Follow-up: recompute the correlation matrix on cadence (weekly?) and write snapshots to
`state/{crypto,equity}_corr.jsonl` for auditability.

Original spec kept below for context.

### (original spec)

The 7-pair `CRYPTO_SLEEVE` runs equal-weight today. A 720-day daily correlation matrix computed
2026-09-02 shows the sleeve is thinly diversified — 5 of 7 pairs cluster on one crypto-beta
factor and equal-weighting is effectively levered long that factor with a small gold hedge:

```
Correlation clusters (Pearson, daily returns, Sep 2024 – Sep 2026):
  BTC · ETH         0.83   ← effectively one bet
  ETH · LINK        0.80
  BTC · LINK        0.73
  XRP · LINK        0.71
  XRP · XLM         0.70

Avg |ρ| with rest of sleeve:
  PAXG/USD  0.17  ← only genuine diversifier (tokenized gold)
  XMR/USD   0.32  ← partial (privacy coin)
  XLM/USD   0.44
  XRP/USD   0.55
  LINK/USD  0.55
  BTC/USD   0.56
  ETH/USD   0.56
```

**Concrete build (small — reuses existing code):**
- `portfolio/allocator.py::correlation_aware` already implements exactly the shape we need:
  positive-score edge budgeting, inverse-vol scaling, correlation-crowding factor (row-sum of
  positive correlations), per-name + per-sleeve caps, regime scalar. The equity path already
  uses it via `portfolio/pipeline.py`. Same allocator, applied to the crypto sleeve.
- `scripts/paper_kraken.py` currently instantiates `LiveMonitor` with per-symbol strategies and
  equal-weight sizing via `PositionSizer`. Wire the allocator upstream: compute allocator weights
  from the 7 pairs' return histories + their `screen_score`, then pass a size cap per symbol to
  the sizer (or replace with an allocator-derived shares target).
- Handle the shallow-history constraint: Kraken OHLC caps at ~720 daily bars — more than enough
  for a stable covariance matrix (need ~60 bars minimum, 720 is comfortable).
- Add `KrakenFeed.correlation_matrix()` or reuse `pandas.DataFrame.corr()` on the aligned close
  frame the correlation-check computed today.

**Expected effect** on sizing (illustrative — depends on the score budget and vol at run time):
- PAXG target weight ↑ meaningfully (low avg ρ, deserves the diversifier premium)
- XMR ↑ modestly
- BTC / ETH / LINK ↓ collectively (they split ~one name's worth of weight, not three names')
- XRP / XLM cluster ↓ similarly

**Guardrails:**
- Keep the sleeve paper-only per the existing `tier="screened"` posture; a correlation-aware
  paper sleeve is a research improvement, not a promotion.
- Enforce the same allocator per-name floor as equity so no pair gets zeroed out entirely — the
  sleeve still monitors all 7 for signal, size just biases toward the diversifier.
- Recompute the correlation matrix on some cadence (weekly? daily-of-the-week?) — crypto
  correlations shift, and a stale covariance is worse than none. Use the same graph-journal
  pattern of writing correlation snapshots to `state/crypto_corr.jsonl` for auditability.

Same pattern applies to the equity paper session — the sleeve currently sizes each of 14 names
independently. Equity correlations aren't as tight as crypto, but a resweep-then-allocator pass
would surface the same "which held names are correlated redundancies" question the crypto
sleeve just answered.

---

## 11. TradeCard — six-axis gap closure  🟠 SKELETON LANDED, INTEGRATION PENDING

**Status (2026-09-06).** An approval-card layer landed uncommitted on the current branch:
`ApprovalRouter` wraps the existing `Router` and blocks each accepted intent on a card-signed
Ed25519 ACCEPT; a laptop shim (stdlib HTTP on `127.0.0.1:8787`) exposes the store; a fake-card
Python simulator round-trips real signatures; an ESP32-S3 firmware skeleton (PCD8544 LCD,
5-key D-pad, NVS-backed passbook) speaks the protocol; a `VSInvestmentEngine` narrates every
intent into a `<=140` char thesis + persisted writeup; `--require-card` toggles the whole
pipeline in `paper_ib.py` and `paper_kraken.py`. 29 tests pass. Nothing is committed.

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
