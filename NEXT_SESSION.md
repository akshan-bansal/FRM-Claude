# Next-session backlog

Queued work, most-ready first. Each item is self-contained enough to start cold. Standing
constraints still apply: new research goes through the **walk-forward gate** before it's wired as
validated; anything that places live orders stays behind the **human go-live confirmation** (paper
/ dry-run first); use `httpx` (not `requests`); `ruff` + `mypy --strict` + `pytest` must stay green.

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

## 3. KrakenBroker — let the Router fill crypto orders  🟡 DRAFT UNTRACKED
A draft `src/trading_live_claude/brokers/kraken.py` exists in the working tree from this session
(uncommitted, not registered in `brokers/__init__.py`, no tests). It implements the `Broker`
protocol against Kraken's public + private REST APIs, with live placement gated behind
`enable_live_orders=True` at construction — the switch is per-instance and no code in the repo
flips it on its own. Pick up from there: finish the tests (mocked with `respx`), register in
`__init__.py`, thread through the `AssetRouter` so crypto orders fill.

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

## 6. GraphRAG / multi-agent overlay on the intel wing  🟡 MVP BUILT
Shipped the "concrete first step" in commit `e475b3c`: `intel/graph.py` — an append-only edge
journal that decomposes every snapshot into typed `(subject, predicate, object, weight, ts)` rows
under `state/intel_graph.jsonl`. Node types (poll, domain, region, source, market), predicates
(observed, elevated_in, co_occurs, stressed_by). Wired into `intel/history.py::append_snapshot`
so every existing caller picks it up for free. Includes an `edge_persistence` query and 10 tests.

**Still to do (the bigger vision):**
- Per-event decomposition when we consume the raw events archive directly (the current MVP uses
  the vendor's already-aggregated snapshot fields — good enough for persistence, thin for
  corroboration).
- SQLite or Neo4j backend when cross-edge queries get interesting; JSONL is fine at MVP.
- OASIS-style specialist reader agents and adversarial thesis debate (off-cadence enrichment
  layer whose outputs the live loop reads).

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

---
_Also on the board (lower priority): wire microprice/OFI as features into the research strategies;
promote the paper A-S maker toward gated live quoting on Kraken._
