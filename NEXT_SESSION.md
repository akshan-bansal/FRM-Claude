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
- **IB sweep — execute the run.** `scripts/sweep_ib.py` is built and CLI-tested. Needs CP
  Gateway auth'd (`https://localhost:5000` browser login) to actually fetch. One command:
  `python scripts/sweep_ib.py`. Writes `reports/ib_sweep_YYYY-MM-DD.{csv,md}`. Bounded work,
  under 10 minutes wall-time for the default universe (~30 ETFs + 5 futures).
- **Item 0 — full resweep** and **item 8 (FX walk-forward)** — both need long-running compute.
  Resweep: ~50 min after cache warm. FX WF over the scan shortlist: needs a
  `walk_forward_pairs.py` wrapper (not built) plus WF runtime.

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
_Also on the board (lower priority): wire microprice/OFI as features into the research strategies;
promote the paper A-S maker toward gated live quoting on Kraken._
