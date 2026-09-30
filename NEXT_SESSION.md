# Next-session backlog

**Pruned 2026-09-16** to active items only. Everything removed was either shipped, run,
superseded, or closed by an explicit user decision; the full pre-prune text is in git history
(last committed version: `4d471f3`) and in the session scratchpad backup
`NEXT_SESSION.backup-2026-09-16.md`.

## Session 2026-09-30 — QuantPort.io basket gate (code-complete, unit-tested; not run live)

Dash renamed QuantPort.io. New **BASKET** tab (first) beside **IB ACCESS**, both ahead of Approvals.
Per venue (kraken / qt / ib) the human types symbols, runs the engines (`analysis/basket_report.py`:
walk-forward record, in-sample screen kept separate, strategy mapping, overlay scalar, interpret
theses, return stats, correlation and allocator weights), and proposes the basket to the card. A
**Router gate** (`execution/basket.py`, `Router.basket_gate`) then rejects ENTRIES outside the
card-signed basket and on venues with none; exits are never gated. Rows in `state/baskets.jsonl`
are re-verified against the card registry on load, so a hand edit approves nothing.

- **Exempt until restart:** running sessions have no gate. `--require-basket` (default: on whenever
  `--require-card` is on) on `paper_kraken.py`, `paper_ib.py`, `cli signal`. After a restart a venue
  takes NO entries until its basket is signed: `python scripts/basket.py sign --venue kraken --from-seed`
  (seed = `config/basket_seed.json`, the asset config settled in the 2026-09-29/30 desktop session).
- **Firmware TODO (not done):** only the SIMULATED card signs baskets (`scripts/basket.py`). The
  firmware parser reads the nine-field trade canonical and refuses `BASKET|1|venue|SYM,SYM|analysis_hash|issued_at|nonce`,
  the safe failure. A physical card needs: a basket prompt kind on the wire (`GET /v1/basket` already
  lists proposals; a signed-row POST does not exist yet), a screen showing venue, symbol count and
  fingerprint, and a `SPEC_VERSION` bump. `firmware/tradecard/main/main.c` was already modified in the tree.
- **Not wired:** QT and IB return statistics (only Kraken pairs in the crypto sleeve fetch closes, via
  Kraken's public OHLC); the panel could not be driven against a live shim from the browser pane, so the
  live render path was checked with stubbed data only.

## Session 2026-09-24 / 25 — defect pass + capital-integrity page

Full suite after the work: **1388 passed, 1 skipped, 0 failed** (4m06s; the skip is the POSIX-only
signal test). Ruff clean on every file touched. Nothing staged or committed by Claude.

### Fixed and verified

- ✅ **`degraded` reported healthy on a stale feed — FIXED 2026-09-24.** `WorldMonitorClient.snapshot`
  set `degraded` only when a tool *raised*. A tool that succeeds but returns a payload it cached days
  earlier left the flag `false`, so a **135-hour-old energy source** was scaling live de-risk at full
  confidence (equity ×0.4669 at the time). Staleness is now measured from each payload's own
  `cached_at` against `MAX_SOURCE_AGE_H = 48.0`, deliberately tied to the existing
  `confluence.STALENESS_HALF_LIFE_H = 24.0` rather than being a second independent policy. Fail-safe:
  tripping it caps every class at `degraded_cap`. 3 tests in `test_intel_overlay.py`.
  **Runtime-exercised** — fired in a live session at `energy: 114.9h`, `degraded: True`.
- ✅ **Intel writeups were structurally empty — FIXED 2026-09-24.** All 19 writeups on disk carried
  `overlay_snapshot`, `market_context` and `warnings` in the schema and empty on disk. The engine was
  never at fault; `scripts/paper_kraken.py` called `explain()` with a bare `MarketContext()` and no
  overlay, while the same script already held a live `OverlayProvider`. Now passes the snapshot, the
  per-symbol decision, and an `r_multiple` derived from the intent. **Runtime-verified** against a
  real prompt: all three fields populated, and the card thesis gained a `geo elevated` clause.
- ✅ **`dashboard.py` fabricated a session — FIXED 2026-09-24.** `.fillna("legacy")` invented a
  session id for the 4 fills that carry none (all 2026-08-31, before `PaperBroker` stamped the field)
  and rendered it as a row beside real ones with no equity curve behind it. Now excluded, with the
  scope stated in the output rather than narrowed silently. Also cleared 4 pre-existing ruff findings
  in that file (verified identical at `HEAD`, none introduced here).

### Considered and deliberately NOT changed

- **Realized P&L excludes commission.** No emitter change. `paper_fills.jsonl` already carries
  `commission` per fill with `session_id`, so net is derivable today. Adding a column would corrupt
  alignment — the equity CSV writes its header only on file creation, so appending wider rows to the
  existing file puts 10 fields under a 9-field header. This is a consumer obligation.
- **Sizing rationale on the Questrade path** (33 of 275 fills). Out of scope by standing rule: sizing
  v2 stays Kraken-only. The coverage gap is that decision visible in the data, not a bug.
- **The 4 fills with no `session_id`** are historical; the live emitter has written it since. Under
  the "unfit data" rule the answer is exclusion, not backfill.

### Open — carried forward

1. ✅ **`paper_ib.py` writeup gap — FIXED 2026-09-25.** Same fix as `paper_kraken.py`: passes the
   live overlay snapshot, the per-symbol decision, and an `r_multiple` derived from the intent.
   **Code-complete, NOT runtime-exercised** — no IB gateway available in this session (the
   interactive-brokers MCP server is also failing to connect), so unlike the Kraken path this one
   has not produced a real populated writeup. Mirrors a fix that was runtime-verified on Kraken.

2. ✅ **Audit ledger — VERIFIED WORKING 2026-09-25. Earlier "writes nothing" was my error.**
   `state/ledger/` looked empty only because no session had yet run with the flag; it defaults ON and
   had simply not been exercised. A card-gated Kraken session then wrote
   `state/ledger/2026-09-25.kraken.jsonl` (10 rows), and `scripts/verify_ledger.py --stream kraken`
   reports **OK** over the hash chain. Full lifecycle present for both intents:
   `STRATEGY_SIGNAL → RISK_CHECK → INTENT_SENT → REJECTED → SIGNED`, each row carrying
   `prev_hash` / `row_hash` / `payload_hash`, plus `signature` and `signing_key_id` on the SIGNED
   rows. `Router.build_default(ledger=...)` does thread through to the emit sites.
   **Residual design note, much weaker than first stated:** `Ledger.append()` is still non-strict by
   default, so a *future* write failure would log rather than raise and would not be obvious from the
   outside. Worth a periodic `verify_ledger` in the runbook rather than a code change; a real
   regulated deployment would construct with `strict=True`.

3. ✅ **Leverage cap now reserves the commission — FIXED 2026-09-25.** `Router._gate` computed
   gross leverage on notional alone, so an order sized to exactly fill the headroom still debited
   the fee on top and overdrew cash. `Router._cost_reserve()` reads `commission_per_trade` off the
   broker (fee structure is broker-specific; the gate must not hardcode one) and the reserve is
   subtracted from the headroom and added to the leverage test. **Only ever tightens the gate.**
   Also exposed `PaperBroker.commission_per_trade` as a property — it was only `self._commission`,
   so both this and `monitor.live_loop`'s existing `getattr(broker, "commission_per_trade")` were
   silently reading nothing and reserving zero.
   **A second bug fell out of the first full-suite run:** the reserve must accept only a REAL
   number, not anything merely `float()`-able. `float(MagicMock())` is `1.0`, so a broker wrapper
   delegating through `__getattr__` — or any test double — fabricated a $1.00 reserve and tightened
   a risk gate on invented input (it broke
   `test_router_risk_gates.py::test_trim_mode_gross_leverage_cap_trims_to_headroom`, trimming to 99
   instead of 100). `_cost_reserve` now rejects non-`int`/`float` values and `bool`, reserving zero.
   4 tests, incl. an order at exactly 100% of equity trimming 1000 -> 999 shares and a
   delegating-broker regression. Unit-tested; not yet seen in a live session.

4. ✅ **OASIS debate output now persisted — DONE 2026-09-25.** `debate()` appends one row per run
   to `state/intel_agents.jsonl`, carrying the `Claim` / `Critique` structure the caller used to
   discard: domain, thesis, direction, self-reported confidence, adversary verdict and reason,
   cited evidence. **Records the attempt, not just survivors** — per-domain `outcome` is one of
   `no_evidence` / `no_claim` / `below_threshold` / `no_critique` / `falsified` / `fired`, plus
   `api_key_present`, so a missing key is distinguishable from a quiet world (the same silent
   no-op the `degraded` flag had). Falsified claims are dropped from the output but KEPT in the
   record, which is what makes "did upheld claims precede better outcomes than falsified ones"
   answerable at all. `journal=False` / `journal_path=` for tests; all 5 pre-existing `debate()`
   calls in `test_intel_agents.py` were switched to `journal=False` so the suite cannot write
   ground truth — verified `state/intel_agents.jsonl` is absent after a run. 4 new tests.
   **Still open:** no claim -> trade -> realized-P&L join yet. `as_of` and domain+thesis are the
   keys to join on; nothing downstream consumes them, so agent accuracy remains unmeasured.

### Analysis corrections — read before reusing any earlier number

- **The "$2,983,231 stranded" figure is retracted.** It summed `positions_value` across 48
  independent $100k accounts, frozen at 11 different as-of dates, over only 13 distinct symbols
  (QQQ in 24 books, EQB.TO in 24), and ~70% of it sat in 15 books whose cash was negative — money the
  accounts never had. It is not exposure, loss, or portfolio value. **Never restate it.**
- **The unfinished-books problem is historical.** All 48 last wrote on or before 2026-09-16; all
  sessions since closed flat.
- **Leverage explains the loss tail.** Peak gross leverage by period: 2.258× (09-01→07, 28 sessions),
  1.111× (09-08→14, 12), 1.000× (09-15→24, 40). The gate at `Router._gate` (`max_gross_leverage=1.0`)
  landed ~09-08. Marking all 48 unfinished books to 2026-09-23 cached closes: **the ten worst are all
  leveraged**; 13 of 48 leveraged overall. Split by leverage, the 35 non-leveraged books total
  **+$13,824** and the 13 leveraged total **−$23,211** — i.e. leverage, not strategy, is why the
  aggregate is negative. Caveat: only 13 symbols across 48 books, so effective sample ≪ 48; the
  direction is robust, the magnitude is not.
- **Closed-book scope** (the only fit scope for rates): 32 sessions, 154 fills, 79 FIFO round trips,
  zero unmatched lots. Winners **28% gross → 10% net**. Commission is flat $4.95/fill.

### Published

- Capital-integrity page: <https://claude.ai/artifact/9thPpNAWXmaHToWUZ6iyj4>
- Partner / diligence brief (TradeCard): <https://claude.ai/artifact/EH9ZFqiFm9oP31MVgjqHTV>

## Status (2026-09-21)

- Branch `feat/multi-scoring-attention-map`, **6 commits ahead of `origin` and NOT pushed**
  (`4d471f3` was the last pushed commit):
  `3b00c28` fix(logging) token leak · `2123b09` fix(intel) threshold calibration ·
  `70a2f8e` fix(strategies) level-trigger strength · `0a13789` fix(schema) venue + AssetClass ·
  `e3f06bd` feat(paper) flatten-on-exit + stop sentinel + warm-up + sizing-v2 default ·
  `92cab0d` docs backlog prune + session report.
  Suite at the last full run (2026-09-21) **1238 passed / 1 skipped / 0 failed** (the skip is the
  POSIX-only signal test). Ruff clean on every file touched; remaining findings are pre-existing
  (`cli.py`, `tests/test_monitor.py`, `tests/test_intel_interpret.py`,
  `tests/test_paper_broker_journal.py`).
- **Uncommitted, Claude's (2026-09-18):** this file, `SESSION_REPORT_2026-09-18.md`, README /
  CLAUDE.md doc fixes, the exit variants (`signals/{profit_lock,candle_exit,overbought_exit}.py`,
  `curves.py`), thesis intensity + heartbeat, the params resolver (`analysis/params.py`), the
  backlog fixes marked "FIXED 2026-09-18" below, and their tests. Nothing staged or committed.
  Futures harness files deleted (unstaged deletions).
- **Uncommitted from 2026-09-14** (user's WIP, not Claude's): desk-policy venue split —
  `src/trading_live_claude/desk_policy.py`, `tests/test_desk_policy.py`, `scripts/paper_ib.py`,
  `scripts/paper_global.py`. This is the **interim** posture:
  IB carries futures/commodities, equities trade on Questrade, no IB FX, one book per currency.
  **IB is staying**, not being removed. Foreign-venue equities and IB FX are held, not dropped
  (see "Held for IB market-data subscriptions").
- **Weekend additions (2026-09-19, not Claude's; kept as given):** crypto random-matrix /
  LSTM-Transformer research, which is the deferred "LSTM" line picked up on crypto:
  `analysis/rmt.py` (Marchenko-Pastur denoised correlation, eigen-embedding),
  `models/lstm_transformer.py`, `scripts/crypto_rmt_lstm.py`, tests `test_rmt.py` /
  `test_lstm_transformer.py`, report `reports/crypto_rmt_lstm_2026-09-19/`, and a `deep` extra
  (`torch>=2.6`, installed 2.14 CPU) in `pyproject.toml`. Research only (no orders). Result on 19
  coins, 216 out-of-sample days: R² vs a zero forecast −0.02, daily rank IC 0.0004 (t = 0.02), so
  no predictive signal yet (a raw-returns ablation ran too). Tests pass in the full suite (1219).
- **Stopping sessions:** `touch state/STOP_<session_id>` (path printed at boot). The session
  exits within one poll and flattens. Never `TaskStop` — it hard-kills and skips the flatten.
  The global `state/STOP` now stops **every** running session (fixed 2026-09-18); a stale one is
  ignored by later launches.
- **Sizing v2 is the Kraken default** (`--no-sizing-v2` to opt out).
- **Warm-up adds nothing (measured 2026-09-18):** 0 of 16 sessions bought after the first poll,
  even with 60 s polling for an hour (daily-bar strategies). Recommendation: drop
  `--warmup-interval` from launches. Open option: a warm-up that ends at the first quiet poll,
  if faster exit checks for the profit lock are wanted.
- **Warm-up cadence (new 2026-09-17):** `--warmup-interval 60 --warmup-minutes 60` on both paper
  launchers polls faster for the first hour, then falls back to `--interval` in the same process
  (no restart, so no flatten). Only ever speeds polling up; 5 s floor; off unless set. Caveat:
  strategies run on daily bars, so warm-up re-checks the forming bar and live quotes — it
  re-establishes positions quickly after a flatten, it does not create new daily signals.
- **Telegram delivery works** (read-only `getMe` / `getChat` OK, 2026-09-18). Thesis alerts had
  gone silent because no reading cleared the 09-16 gates. **Gates lowered 2026-09-18 by user
  decision** to `strategic_risk >= 67` / `conflict_events_active >= 4` (base rates on real reads
  ~81% / ~70%; see the `interpret.py` header). The same constants drive the live-loop trim, so
  CGL.TO / PAXG/USD entries get ×0.75 on most reads. The graph journal now sends a **heartbeat**
  (first poll after launch, then every `--heartbeat-hours`, default 24). Code-complete and
  unit-tested; not yet runtime-exercised, since the graph journal was stopped before a relaunch.

## Next session — ordered work list (set by the user 2026-09-18)

Work these in order. Each links to its detail further down.

1. **Flatten-on-exit gaps.** ✅ **Journal rehydration DONE 2026-09-21:** `PaperBroker.resume()`
   replays a session's fills through the same accounting as live fills, cross-checks cash and
   realized P&L against its last equity row (refuses on mismatch), restores peak equity, and
   continues order ids. `--resume-session <id>` on `cli signal` and `paper_kraken.py` (not yet on
   `paper_ib.py` / `paper_global.py`, which are the desk-policy WIP). Restart recipe: stop with
   `--no-flatten-on-exit`, relaunch with `--resume-session <id>` and the same `--paper-equity`.
   5 unit tests. Runtime check, read-only against the real journals: session `ec72c96f` replays to
   the journal's cash/realized exactly, and **all 48 orphaned books replay cleanly**.
   **Your call:** resume them to close them (that books today's prices into old sessions) or leave
   them marked unfinished. Still open under #1: Also: the card-gated flatten
   (`--require-card` makes exits need an ACCEPT), the unified `_book_risk`, and a policy for
   rejected flattens. Detail: "Flatten-on-exit: remaining gaps".
   **Why it's first (measured 2026-09-18):** flatten-on-stop plus relaunch re-bought a name just
   sold 25 times on 09-17/18, often 1–16 minutes later, at $9.90 per round trip plus ~0.1% in price.
   Commissions on those sessions were $297; QT's +$53.61 gross on 09-18 became −$65.20 net.
   Rehydration lets a restart keep the book instead of selling and re-buying it. Until then, avoid
   mid-day restarts (`--no-flatten-on-exit` without rehydration orphans the book).
2. ✅ **Token-store salt migration — DONE 2026-09-21** (code-complete, unit-tested, checked on a
   copy). New file format `TLC2$` + 16-byte random salt + Fernet token; the legacy fixed-salt file
   is still read. No separate migration step: the next ordinary token refresh rewrites the file in
   the new format, and the `.bak` keeps the legacy copy, which stays readable. Checked on a copy of
   the real `tokens.json.enc`: legacy readable, rewritten, reads back identical, `.bak` fallback
   works; the real files are unchanged (sha256 before/after). 4 new tests. **Not yet
   runtime-exercised:** the first real refresh with this code is the live test.
3. ✅ **`apply_overlay` WIRED 2026-09-21** (your call; code-complete, unit-tested, not yet
   runtime-exercised). `intel.apply.OverlaidBias` wraps the boot-time allocation and, on every
   call, runs `apply_overlay` with the overlay's *current* class decisions, so the allocator bias
   carries the OSINT class scalar and follows the 15-min refresh. It is marked `applies_overlay`,
   and `LiveMonitor` then multiplies in only the strategy-risk part of `combine()`; the halt and
   its reasons still come from the full combination, so the overlay is applied **once**. Wired in
   `cli signal` (QT) and `paper_kraken.py`, only when the overlay is on and the allocator produced
   weights; otherwise the old in-loop path runs unchanged. **Not wired in `paper_ib.py` /
   `paper_global.py`** (desk-policy WIP; `paper_ib`'s bias also carries the bond-hedge rule).
   Known difference: the loop's [0.1, 3.0] bias clamp now applies to bias x overlay rather than
   to the bias alone, which only matters when their product falls below 0.1. 3 new tests
   (conviction identical to the old in-loop path, halt kept, bias follows a mid-session refresh).
4. **Dependency caps in `pyproject.toml`** (no upper bounds; `ib_insync` unmaintained). Cap at
   the next major version from the lockfile, and note `ib_insync` -> `ib_async`.
5. **Signal-system plan phases: V4 (trend pullbacks), then calibration.** "Plan — robust
   entry/exit signal system", Phases 2 and 3. Phase 0.1 (params resolver) is built; its default
   flip (`--params wf`) is still your call.

Open decisions carried alongside (not ordered): the `QUESTRADE_ENV` / autonomous account guard
(🔴), `--params` default, CI scope, `reports/` tracking policy, the QT commission reserve.

## Guardrails — standing rules and closed decisions (do not reopen unprompted)

- **Data-first (2026-09-08).** Accrue before tuning, promoting, or wiring gates. A single WF pass
  validates the *protocol*, never promotes a tier. Don't wire microstructure gates until ≥90 days
  of density. No new autonomy or scheduled tasks without asking.
- **Microstructure controls beyond top-of-book: HELD for L2 market data (2026-09-16).** This
  replaces the 2026-09-09 "omitted" decision; see "Held for market data" below. Don't build the
  accumulator, `LiquidityGate` or depth-aware controls before L2 data is available.
- **Alerter: no changes (2026-09-09, reaffirmed 2026-09-17).** Don't propose Telegram alerter
  dedup or other edits. On 09-17 the user also declined adding an HTTP-status check to
  `_telegram`, so rejected sends stay silent by choice. Verify delivery out-of-band instead
  (read-only `getMe` / `getChat`, or the user's phone).
- **FX pair-trading: not tradeable here (2026-09-04).** Cost drag is 25–75% of the daily FX
  excursion. Only revisit with a sub-pip cost model or `KalmanPairs`.
- **FX single-name sleeve: dropped (2026-09-05).** 0 fills in 14 polls. Only revisit with a
  sub-minute FX vendor or materially looser FX thresholds. FX deep parquets stay on disk.
- **`LQD`, `MUB`, `DBA` excluded from selection (2026-09-04).** Don't re-propose.
- **No sizing v2 on QT (2026-09-16).** Sizing v2 stays Kraken-only (`scripts/paper_kraken.py`).
  Don't add it to `cli.py signal` or propose porting it to the QT path.
- **Client Portal Gateway** (`Downloads/clientportal.gw`): never leave it authenticated to the
  LIVE account. `conf.yaml` has `origin.allowed: "*"` and there's no read-only mode.

## Open decisions — 🔴 user call

1. **Rotate the IBKR OAuth token.** Its value was committed (`638f8d3`) and pushed to
   `origin/feat/multi-scoring-attention-map` and `origin/feat/tradecard-approval`. Now redacted
   here, but still in git history.
2. **The kill-switch is effectively 8%.** `config/trading.yaml` sets
   `max_drawdown_kill_switch: 0.08`, overriding the 0.03 default landed 09-08.
3. **The real-money paths lack the paper wiring.** `trading live` and `autonomous_run` build
   `LiveMonitor` without the overlay / interpret / allocator / persistence hooks, the stale-quote
   guard, session routing, flatten-on-exit or the stop sentinel.
4. **`Router.check_forced_exits` has no caller** outside tests, so the "cheap" intraday
   loss-exit never runs.
5. **The QT paper book mixes CAD and USD names** without conversion (QQQ, VALE, DBC beside `.TO`).
6. **TradeCard signs neither the thesis nor the stop**, and the stop isn't displayed.
7. **Graph-weighted interpret default** — unanswered. The proposal is to keep it behind a flag,
   off, until weeks of graph polls can test whether persistence predicts anything.
8. **Should thesis intensity drive anything?** `intel/thesis_intensity.py` (2026-09-18) grades
   each firing thesis 0–1: bounded `log10(1 + 9u)` curves over each input's value (neutral floor
   → stress ceiling, per-input bounds in `MAGNITUDE_BOUNDS`) and over hours firing continuously
   (0–72h, a fresh fire weighted 0.5). OR theses take their strongest input, AND theses their
   weakest. It is **display-only**: shown in thesis alerts and the heartbeat, but it doesn't
   change firing, confidence bands or the sizing trim. Code-complete and unit-tested; not
   runtime-exercised (the graph journal is stopped). Corpus replay: intensity p50 ≈ 0.29–0.39,
   max 0.50, because firing streaks never exceeded 7h in the record, so the time factor has
   never passed ~0.6. Options: map intensity to the confidence band, make the ×0.75 / ×0.5 trim
   continuous, or require a minimum intensity before alerting.

## Plan — robust entry/exit signal system for the QT book, by strategy family (drafted 2026-09-18)

**Scope (user decisions 2026-09-18): the entry/exit V-layer (V1–V4) applies to the QT equity-venue
book, all 14 names.** That includes DBC (`commodity`) and CGL.TO (`precious_metals`), which trade on
Questrade even though `classify_symbol` doesn't put them in the equity class. Crypto (Kraken) keeps
its own strategy entries and exits and gets no V-layer.

**Goal.** One signal policy that decides, per strategy family, which entry and exit legs run on an
equity name and with which parameters, every choice backed by pinned-parameter walk-forward
evidence net of realistic costs. It replaces today's hand-set CLI flags (`--profit-lock-exempt
ts_momentum` ...). The standing QT config (V1 + V2 + V3 with `ts_momentum` exempt) keeps running on
paper meanwhile as the interim policy.

**What the 2026-09-18 evidence already says** (in-sample, one config; hypotheses, not settled):
profit-taking exits (V1 lock, V2 bearish candle, V3 overbought) help mean-reversion and channel
names (XIU.TO, VALE, XIC.TO, RSI.TO) and destroy trend names (QQQ +97% -> -1%). Candles help
mean-reversion *entries*, not exits. Every "win" so far came partly from less time in market, so
return and exposure must be scored alongside sortino/DD.

### Ground rules (every phase)
- **Scoring:** OOS `sortino_over_dd` is primary, always shown with OOS Sortino, OOS return and time
  in market (a leg that just sits out can "win" the ratio; V1 on all 14 names did).
- **Costs:** realistic per-name costs (`CostModel.from_price` + $4.95/fill) **and a 2x cost stress
  gate**. Nothing is adopted that loses to the baseline at 2x.
- **Pinned-parameter walk-forward** (as in `scripts/calibration_sweep.py`, not the re-optimizing
  `sweep_universe.walk_forward`), using the equity `WF_PROTOCOLS` (2y train / 6mo test / 10 OOS
  trades), so every OOS number is attributable to a specific parameter value.
- **Small a-priori grids**, a count of configs tried, and a noise band: a winner has to beat the
  baseline by more than the fold-to-fold spread, not just on the median.
- **Fold policy** (09-16 precedent): adopt only with WFE >= 1.0 and enough OOS trades. Otherwise
  the cell stays "strategy exit only".
- **Data-first guardrail:** one WF pass validates the protocol; promotion also needs the paper A/B
  (Phase 5).

### Phase 0 — Foundations (blocking; do first)
1. **Fix the QT params path (existing red item "live paper path runs STOCK CLASS DEFAULTS").** Add a
   `resolve_params(strategy, symbol, mode)` chain: CLI -> WF registry -> `calibrated_kwargs` ->
   defaults. 13 of the 14 QT names have registry params, but QT runs defaults. Calibrating exits on
   top of the wrong entry params would tune the wrong system. Re-run the 09-18 exit backtests on
   registry params afterwards.
2. **Exit-reason attribution in the backtest engine.** Tag each exit (strategy / stop / lock /
   candle / overbought) and each V4 entry, so each leg's contribution is measured directly instead
   of inferred from A/B deltas.
3. **One `SignalPolicy` object** (strategy family -> legs + params) resolved per symbol for the QT
   book (all 14 names, DBC and CGL.TO included), replacing the per-leg exempt flags. Kraken names
   resolve to "no V-layer". Journal the resolved policy per intent in `paper_orders.jsonl`.
4. **Data:** refresh the equity cache (`scripts/warm_cache.py --held --seed equity --years 5`).
   Consider widening the equity basket beyond the 12 book names with the other WF-registry
   equities (32 registry names total), so each family cell has more than 2–5 names. Finish the
   graph-journal test leak (spawned session) so no corpus is polluted.

### Phase 1 — Cells to calibrate (QT book, 14 names)
- **Families present in the book:**
    * trend: ts_momentum (EQB.TO, QQQ, VDY.TO);
    * channel: atr_channel (ZEB.TO, CGL.TO);
    * mean reversion: bollinger (VALE, DBC, SLF.TO, RSI.TO), rsi_meanrevert (XIC.TO, CRT.UN.TO),
      confirm_* (ENB.TO, SRU.UN.TO);
    * composite (XIU.TO), evaluated as its own cell.
  DBC and CGL.TO calibrate within their strategy family. Report their per-name result separately,
  since they're the only commodity and gold names.
  Widen each family with registry equities (Phase 0.4) so no decision rests on 1–2 names.
- **Starting hypotheses:**
    * trend: strategy exit only (or a wide Chandelier trail), no V1–V3; V4 pullback buys inside an
      up-trend;
    * mean reversion: V1–V3 candidates, candle confirmation on entries, no V4;
    * channel and composite: test both ways.

### Phase 2 — Build V4 "buy on oversold" (user idea 2026-09-18: win back the return V1–V3 give up)
- **Trigger (mirror of V3):** on the last completed bar, close below the lower Bollinger band
  (20, 2 sd) or RSI(14) <= 30. Optional bullish-reversal candle confirmation, the mirror of V2 and
  the one place candles measurably helped.
- **Role (user decision 2026-09-18): trend pullbacks only.** For trend-family names, buy oversold
  dips only while the trend filter is up (the strategy's own signal is on, e.g. 126-day ROC > 0), so
  it never catches a falling knife in a downtrend. It adds exposure in names that keep their
  strategy exit, offsetting the return the V1–V3 exits give up elsewhere. **No re-entry role:** the
  re-entry lockout after V1–V3 exits stays as is.
- **Guards:** the ATR stop is mandatory on every V4 entry; at most one V4 entry per symbol per N
  bars; normal sizing and the Router; trade count and cost drag reported for every arm.
- **Tests:** no lookahead, the trend filter (no buys while the trend is off), the cooldown, live
  completed-bar reading, and the entry tag in alerts.

### Phase 3 — Calibrate on data (pinned-param WF per family cell)
- **Grids (a priori, small):**
    * V1: `arm_atr` {0.75, 1, 1.5} x `giveback_min_atr` {0.75, 1, 1.5} x `full_atr` {3, 4, 6};
    * V2: pattern set {8 mirrors, strong 3-bar only} x `min_gain_atr` {0.5, 1, 2};
    * V3: `rsi_level` {70, 75, 80} x `bb_std` {2, 2.5};
    * V4 (trend family only): `rsi_level` {25, 30, 35} x `bb_std` {2, 2.5} x candle confirm {on,
      off}. The trend filter is always on.
  Strategy core params come from the WF registry (Phase 0.1) and are **not** re-tuned here, to keep
  the search small.
- **Per family cell:** arm "strategy only" versus each leg, pairs of legs, and V1–V3 (+V4). Take the
  median across the family basket of OOS sortino/DD, with Sortino, return, time in market and
  trades beside it, at realistic and 2x costs.
- **Adoption rule (tolerances confirmed by the user 2026-09-18):** beat "strategy only" on OOS
  sortino/DD by more than the noise band; OOS Sortino no worse than -0.05; return give-up <= 3 pp
  per year; WFE >= 1.0; enough trades; still ahead at 2x costs.
- **Output:** `reports/signal_policy_calibration_<date>.{csv,md}` plus the `SIGNAL_POLICY` table in
  code, with provenance (the report and the numbers) in comments.

### Phase 4 — Integrate (QT book)
The resolver picks each QT symbol's policy from its strategy family, and Kraken names get none. CLI flags become overrides only. Alerts name the leg that fired. The boot banner prints
the resolved policy per symbol.

### Phase 5 — Validate on paper (promotion gate)
- **Concurrent A/B:** two QT paper books on the same equity names at the same time, one on the
  calibrated policy and one on strategy exits only. Running them together removes the regime
  confound that sequential sessions have.
- **Length:** at least 4 weeks. Compare per book on sortino/DD, Sortino, return, time in market and
  trade count. Add a book tag to the journals.
- **Promotion:** only if the policy book holds up.

### Decisions (2026-09-18) and what is still open
- **Resolved:** DBC and CGL.TO are in the V-layer; V4 is trend pullbacks only; the adoption
  tolerances (-0.05 Sortino, <= 3 pp/yr return) stand.
- **Still open:** should the Phase 5 concurrent A/B run two QT paper books (doubling alert volume)?

## Runbook — resume from cold

```
# 1. Tests
.venv/Scripts/python.exe -m pytest tests/ -q --no-cov --ignore=tests/test_quantconnect.py

# 2. Refresh cache if stale
.venv/Scripts/python.exe scripts/warm_cache.py --held --seed equity --years 5

# 3. Pre-flight: kill-switch clear and no stale stop files (a stale STOP kills new sessions)
ls state/HALTED state/STOP* 2>/dev/null

# 4. Paper monitors (flatten-on-exit is the default on both; sizing v2 is the Kraken default)
.venv/Scripts/python.exe -m trading_live_claude.cli signal --strategy bollinger \
    --symbols "XIC.TO,ZEB.TO,CGL.TO,VALE,DBC,SRU.UN.TO,CRT.UN.TO,ENB.TO,XIU.TO,VDY.TO,SLF.TO,RSI.TO,SONY,EWJ,FLJP,JPXN,NVDA,AVGO,AMAT,MU,INTC,QCOM,TXN,ADI,NXPI,ON,SMH,SOXX" \
    --strategy-map "XIC.TO=rsi_meanrevert,ZEB.TO=atr_channel,CGL.TO=atr_channel,VALE=bollinger,DBC=bollinger,SRU.UN.TO=confirm_rsi_meanrevert,CRT.UN.TO=rsi_meanrevert,ENB.TO=confirm_bollinger,XIU.TO=composite,VDY.TO=ts_momentum,SLF.TO=bollinger,RSI.TO=bollinger,SONY=rsi_meanrevert,EWJ=bollinger,FLJP=bollinger,JPXN=rsi_meanrevert,NVDA=bollinger,AVGO=bollinger,AMAT=bollinger,MU=rsi_meanrevert,INTC=bollinger,QCOM=bollinger,TXN=bollinger,ADI=bollinger,NXPI=bollinger,ON=bollinger,SMH=bollinger,SOXX=bollinger" \
    --interval 300 --paper --paper-equity 100000 --level --intel-overlay
.venv/Scripts/python.exe scripts/paper_kraken.py --interval 300 --paper-equity 100000

#    Optional warm-up (2026-09-17): poll faster for the first hour to re-establish positions after a
#    flatten, then fall back to --interval in the same process. Add to either command:
#      --warmup-interval 60 --warmup-minutes 60
#    QT's current map runs ENB.TO=confirm_bollinger, SRU.UN.TO=confirm_rsi_meanrevert, XIU.TO=composite.
#
# 4c. ADDED 2026-09-25 (user): Japan names + chipmakers. 16 new names, QT watchlist is now 28.
#     Strategy per name chosen by pinned-param WF over 5 a-priori strategies x 10y of bars
#     (reports/chip_japan_wf_2026-09-25.md; 63 of 120 cells cleared the gate, 20 of 24 names at
#     best-of-5). Japan: SONY=rsi_meanrevert, JPXN=rsi_meanrevert, EWJ/BBJP/FLJP=bollinger.
#     Chips: ASML and MU = rsi_meanrevert, the other 13 = bollinger.
#     HELD BACK, too few OOS trades: TSM (7), KLAC (8), ARM (5 over 2 folds), BBJP (8) — BBJP is the
#     5th Japan name asked for, held at 8 OOS trades against a 10 minimum.
#     EXCLUDED on OOS return despite clearing the score/WFE/trades rule: MRVL (-4.87% on a 27% win
#     rate), AMD (-0.49%), LRCX (-1.83%). The tier rule never asks whether the name made money.
#     EXCLUDED on degradation: ASML (WFE 0.059 on live params).
#     *** The selection sweep ran DEFAULT params, but `--params wf` resolves every one of these names
#     to CALIBRATED params (bollinger 30/1.5, rsi_meanrevert 15/35) — a different configuration from
#     the one scored. Re-scored on the live params
#     (reports/chip_japan_live_params_2026-09-25.md): 16 of 20 survive, and AMD + ASML flipped from
#     robust to failing. Never promote on a sweep whose params are not the ones the book will run. ***
#     CAVEATS: (a) best-of-5 selection is biased upward — treat the ranking as a shortlist, not
#     evidence; (b) `bollinger` won 15 of 20, which is a suspiciously uniform answer; (c) several
#     WFEs are 5-32x, which means the IN-SAMPLE score was tiny, not that OOS was excellent;
#     (d) single-stock semis carry 20-60% OOS max drawdowns (ON -61.7%, NXPI -46.8%, AVGO -41.2%)
#     against an ETF book that ran -5 to -10% — the vol-scaled cap will size them smaller and the
#     50% clamp binds, but this changes the book's risk character; (e) 32 names compete for 6 slots,
#     so this adds candidate competition and alert volume, NOT exposure.
#
# 4a. OMITTED 2026-09-25 (user): QQQ, EQB.TO, LMT, RTX are out of the QT book. All four are
#     ts_momentum and all four scored 0 with ZERO out-of-sample trades across 6-13 folds in the
#     pinned-param walk-forward (reports/full_sim_wf_2026-09-25.md) — a 126-bar test window is
#     shorter than the strategy's own 126-189 bar lookback, so no trade ever completes. Their WF
#     numbers are evidence of nothing, in either direction. Note what this leaves: VDY.TO is the
#     only ts_momentum name still in the book (OOS 41.3, WFE 9.9, but 6 OOS trades = 'watch'), and
#     it was the biggest loser on both live sessions this week. ITA/NOC/GD stay for now on
#     defaults with 388 bars, which is too little for a single fold.

# 4b. Exit variants — STANDING config (user decision 2026-09-18): add to the QT command above.
#       --profit-lock --profit-lock-exempt ts_momentum --candle-exit --candle-exit-exempt ts_momentum --overbought-exit --overbought-exit-exempt ts_momentum
#     V1 + V2 + V3 on every strategy except ts_momentum. Backtest: "Exit variants" below.
#     2026-09-22 (user): bundle V4 with them — add --oversold-entry --oversold-entry-only ts_momentum
#     (code-complete, unit-tested, NOT walk-forward calibrated; adds a tranche to a HELD trend name).
#     Also new 2026-09-22: --parallel-sizing (default ON, QT + Kraken), --mute-alerts RSI.TO (for now),
#     defense names ITA,LMT,RTX,NOC,GD on the QT watchlist as ts_momentum (no WF evidence: LMT/RTX
#     "watch" tier in reports/wf_symbols_defense_2026-09-22.md, ITA/NOC/GD uncached), and
#     --no-flatten-on-exit on every launch (user rule 2026-09-22: never flatten without an ask).
#     QT cap raised to 5 (user 2026-09-22): add --max-positions 5 to the QT command (Kraken stays on
#     trading.yaml's 3). --trim-to-slots is ONE-SHOT (ran 16:15 UTC on fba831e3: every holding x0.80,
#     pro rata); keep it off the standing command or every restart trims again.
#     (Partial-sell realized_pnl bug: FIXED 2026-09-24, see "Queue — correctness bugs".)

# 4c. Restart WITHOUT closing the book (2026-09-21): stop the running session with its flatten off
#     (launch it with --no-flatten-on-exit), then relaunch the same command plus
#       --resume-session <session_id>        (same --paper-equity as the original)
#     The launch refuses if the journals disagree; nothing is re-bought.

# 5. Graph journal
.venv/Scripts/python.exe scripts/graph_journal.py --iterations 96 --sleep 900 --wash-min-hours 72 --persistence-threshold 5

# 6. Stop a session cleanly (per session — see the global-STOP bug)
touch state/STOP_<session_id>
```

Notes: `uv` isn't on PATH on this machine; use `.venv/Scripts/python.exe`. QT launched after
16:00 ET opens nothing. ARX.TO and RIG.TO were removed for 404-on-candles; pre-flight symbol
validation now catches that class of failure at launch.

## Queue — correctness bugs

- ✅ **Paper broker didn't book realized P&L on a partial sell — FIXED 2026-09-24.** `_apply_fill`
  now realises `(fill − avg) × closed_qty` on any reducing fill (average entry unchanged), handles a
  fill that crosses zero, and keeps a legacy full-closes-only accumulator so `resume` accepts a
  journal written before the fix instead of refusing to start — `state/` is not rewritten, and the
  corrected figure is used with a warning. Replaying real session `fba831e3` reproduces the measured
  **−$39.92** of unbooked P&L exactly. Tests: partial books P&L, partial+close == one close, resume
  round-trip, legacy journal accepted, garbage journal still refused. **Sessions started before the
  fix keep the old behaviour until restarted.** Original report:
  `brokers/paper.py` `_apply_fill` adds to `_realized_pnl` only in the `new_qty == 0` branch (a full
  close). A partial reduction lowers `openQuantity`, keeps `averageEntryPrice`, and moves the
  proceeds into cash, so **equity is correct**, but the sold shares' P&L never reaches the
  `realized_pnl` column of `paper_equity.csv`. That column then understates realized P&L.
    * **Who does partial sells now:** `LiveMonitor.trim_to_slots` (`--trim-to-slots`, new
      2026-09-22) and the V4 tranche stop (`live_loop.py`, `qty = min(qty, self._v4_tranche[...])`).
      Strategy exits, profit lock, V2/V3 and flatten all sell the full quantity, so they are unaffected.
    * **Measured:** QT session `fba831e3…`, slot trims at 16:15 UTC (orders 5–8). Realized P&L not
      booked: **−$39.92** (EQB.TO +0.62, QQQ −4.46, SRU.UN.TO −4.31, VDY.TO −31.77). Its equity rows
      are right; its `realized_pnl` reads 0.00 where it should read −39.92 (before commissions,
      which the column excludes anyway).
    * **Fix:** in the reducing branch, realize `(fill − avg) × closed_qty` (sign-flipped for a
      short) and leave the average entry unchanged. Also handle a fill that crosses zero (close
      plus reopen); long-only today, so it can't happen yet.
    * **Resume interaction — why it wasn't fixed on the spot.** `resume()` replays fills through the
      same `_apply_fill` and raises `RehydrationMismatch` when replayed cash or realized P&L differs
      from the session's last equity row by more than $0.05. Today both sides use the buggy path, so
      they agree. After the fix, replaying `fba831e3` computes −39.92 against a journal row that says
      0.00, and **`--resume-session fba831e3…` refuses to start**. Options: (a) fix once `fba831e3`
      has closed; (b) make the realized check tolerate rows written before the fix (a journal
      version marker, or check only cash, which the fix doesn't change); (c) fix, then start a
      fresh session. Don't rewrite the journal rows: `state/` is ground truth.
    * **Tests to add:** partial sell books realized P&L; a partial sell then a full close sums to
      the same total as one full close; resume round-trips a session with partial sells.

- ✅ **TradeCard approval axis in paper mode — RESOLVED, verified end to end 2026-09-18.** The fixes
  were already in the uncommitted TradeCard WIP. `approval_asgi.Broker` now includes `paper`,
  `ib_web` and `global`, and prompts carry the broker's `.venue`. `approval_card_sim._get` decodes
  non-JSON error bodies instead of crashing. Re-test (session `c49d975a…`, `paper_kraken.py
  --require-card`, card sim on auto-accept):
    * entry published → card ACCEPT → `paper.order.filled` Buy PAXG/USD 5.986;
    * the flatten Sell published → ACCEPT → filled; book flat (−$35.95).
  Both intents are in `state/approval.db` with `verdict=ACCEPT`, `broker=paper`, `consumed=1`,
  signer `card-sim-001` and canonical `paper|…`. The shim had exited, so this was read from the DB,
  not `/v1/passbook`. No 500s. `CLAUDE.md`'s recipe was rewritten to the verified commands.
  `SPEC_VERSION` is still 1.0.0; bump it if the firmware allowlist needs to learn `paper`.

- 🟡 **UPDATE 2026-09-18 — mechanism built, default unchanged pending your call.** `analysis/params.py` `resolve_params` / `build_strategy` implement the precedence below (steps 1–3):
  `cli.py signal --params {default,wf,calibrated}`, default `default`, so QT runs exactly as before
  until you flip it. The boot banner prints each symbol's source and running params. Entry alerts
  now show "Evidence is for: …" (registry) and "Running: …" (the live instance) and flag a MISMATCH.
  Unit-tested (`tests/test_params_resolver.py`); not runtime-exercised. With `--params wf`, 10 of 14
  QT names get registry params (e.g. QQQ `lookback=189, threshold=0.02` vs default 126 / 0.0); the
  `confirm_*` names get calibrated (their symbol reaches the pattern filter at last); `composite`
  stays on defaults. **Still open:** (a) your decision to flip the default to `wf` (it changes what
  QT trades and breaks comparability with the 09-18 exit-variant backtests, which ran defaults);
  (b) journaling the resolved params per intent in `paper_orders.jsonl` (lives in `brokers/paper.py`,
  which the graph-leak session is editing); (c) `confirm_*` wrappers can't yet take their base
  strategy's registry params; (d) the A/B in step 4.
- 🔴 **The live paper path runs STOCK CLASS DEFAULTS, not WF-validated or calibrated params, and
  the alerts misreport this.** Found 2026-09-16. Verified by inspection:
    * `cli.py` `_strategy_or_die(name)` returns `STRATEGIES[name]()`, zero-arg, and the
      `--strategy-map` build calls it for every symbol.
    * `analysis/calibration.py::calibrate_for` has one caller in the repo: `tune.py`.
    * `intel/notification.py` renders the alert's `Strategy: … with <params>` line from
      `wf_record.params` (the registry), never from the running instance.

  Measured on QT session `b14e4de0…`:

  | symbol | alert claimed (registry) | actually running | calibrated_kwargs |
  |---|---|---|---|
  | `SRU.UN.TO` | `window=7, oversold=25` | `window=14, oversold=30` | `window=15, oversold=35` |
  | `VDY.TO` | `lookback=63, threshold=0.02` | `lookback=126, threshold=0.0` | `{}` |
  | `EQB.TO` | `lookback=126, threshold=0.0` | same (coincidence) | `{}` |

  Only 3 of 14 symbols were sampled. **Consequence:** `state/paper_fills.jsonl` is *not*
  evidence about WF-validated configs, and every paper alert to date may describe a config that
  never traded.

- **An explicit parameter-precedence chain, so the calibration fold can be A/B tested.** Depends on
  the bug above. Taken literally, "WF params pull from `calibrated_kwargs`" would overwrite
  per-symbol walk-forward evidence with class-level medians and recreate the misreporting bug. Plan:
    1. `intel/notification.py` renders the *live instance's* params. Keep the registry OOS/WFE
       numbers, labelled as evidence for the registry params, and show the mismatch when it exists.
    2. One resolver, `resolve_params(strategy_name, symbol, mode)`, with the order: CLI override →
       WF registry → calibrated_kwargs → class defaults.
    3. A `--params {wf,calibrated,default}` flag on `cli.py signal` and the paper scripts. Journal
       the mode and the resolved params per intent in `paper_orders.jsonl`.
    4. A/B: `--params default` vs `--params calibrated` on the same symbols, ideally interleaved
       or run concurrently (regime confound). **Not** promotion evidence; WF stays the gate.

- ✅ **Graph-journal wash cadence resets on every restart — FIXED 2026-09-18.** `intel/graph.py` `last_wash_time` / `record_wash_time` persist it (`<journal>.last_wash`, falling back to the `.bak` mtime); `graph_journal.py` reads it at boot. Unit-tested; the real journal reads the 14:55 wash (4.6h ago), so the next launch won't prune.
- ✅ **`--max-prune-fraction 0.0` pruned everything instead of nothing — FIXED 2026-09-18.** `wash_due()` treats <= 0 as "no wash" (and leaves `.bak` alone); `None` still means uncapped. The boot banner says when pruning is disabled. Unit-tested.
- 🟡 **The test suite was writing into `state/` ground truth.** Traced 2026-09-18:
    * **Overlay journal: FIXED (uncommitted).** Three `OverlayProvider(...)` calls in
      `tests/test_intel_overlay.py` used the default `journal=True`, so every run appended empty
      snapshots (`global_alert_count 12`, `market {"crypto_chg": 9.0}`, `degraded: false`) to
      `state/intel_overlay.jsonl`. They now pass `journal=False`, and a full run leaves the file
      unchanged (341 → 341). About 89 of 341 rows are empty or degraded, most of them from this
      leak; they diluted the 09-16 calibration's base rates. The rows are left in place: cleanup
      needs your approval.
    * **Graph journal: OPEN.** A full run still adds ~33 fake `traded` edges (venues `fake`,
      `static-feed`, `my-custom-venue`, …) to `state/intel_graph.jsonl` through the paper broker's
      `fill_edge` append. There's a spawned task for it ("Stop tests writing fill edges to the real
      graph").

- ✅ **The global `state/STOP` stopped only ONE session — FIXED 2026-09-18.** It is no longer consumed; each session honours it only if its mtime is at or after that session's start (2 s grace for Windows mtime granularity), so every running session stops and later launches ignore it. `STOP_<id>` is still consumed. Tests: both sessions stop, a stale file is ignored, the per-session file is consumed. `HALTED` untouched.

- 🟡 **Flatten-on-exit: remaining gaps.** Built 2026-09-16 (`LiveMonitor.flatten`,
  `request_stop`, `--flatten-on-exit` on `cli.py signal` and `paper_kraken.py`; stop-sentinel
  exit). Verified flat on `f082cdb1…`, `889dde54…`, `b5870348…`, `9e834d13…`. Still open:
    * Not wired into `paper_ib.py` / `paper_global.py` (the user's desk-policy WIP; coordinate).
    * `PaperBroker.__init__` starts flat with **no journal rehydration**, which is in tension with
      "`state/` files are ground truth". A crashed session's book can't be recovered or closed.
    * `_book_risk` duplicates `step()`'s inline risk block. Unify.
    * **With `--require-card`, the flatten-on-exit sells need a card ACCEPT too** (seen in the
      2026-09-18 E2E). If the card is offline or declines at stop time, the flatten prompt expires
      and the book stays open. Decide whether exits bypass the card (they reduce risk) or wait.
    * A flatten can be **rejected** by the kill-switch, the min-ticket floor or the heat cap (they
      apply to SELL). It's reported loudly but not retried. Decide the policy.
    * ✅ `PaperBroker` journal rehydration — built 2026-09-21 (see the ordered list, item 1).
    * Orphaned books in the journals — now **recoverable** via `resume()` (48 of 48 replay cleanly,
      2026-09-21); closing them is your call (it books today's prices into old sessions):
      `4a6ad96a`, `982b7458`, `b14e4de0`, `eeefb9e2`, `ca1252ad`, `dbefdbe2`, `e6c192ef`,
      `1af5bb80`, plus earlier sessions of the same shape.

- ✅ **`classify_symbol` routed `EUR/USD` to crypto — FIXED 2026-09-18.** Slash pairs with two fiat legs are `fx`; crypto slash pairs are unchanged. Regression test covers EUR/USD, USD/CAD, EURUSD, BTC/USD, BTC/EUR, PAXG/USD, BTC-USD, /ES.

- 🟡 **CI runs a hardcoded test list — NEEDS YOUR DECISION (2026-09-18).** The workflow is deliberately TradeCard-only (its header says the full suite touches network-facing IB/Kraken paths). Proposal: a second job on `ubuntu-latest` running `pytest tests/ --ignore=tests/test_quantconnect.py` with the full dev install. Unverified here: the POSIX-only daemon test (skipped on Windows) would run for the first time, and any test doing real network I/O would surface. Needs a push to try, so it's your call.

- 🟢 **QT paper cash goes a few dollars negative (diagnosed 2026-09-18, left as is).** Four first-poll buys fill to ~99.99% of equity because the router's gross-leverage cap (1.0 × equity) doesn't count the $4.95/fill commissions (session `ec72c96f`: $99,985 notional + $19.80 → −$5.24 cash). Realistic for a margin account and cosmetic. The fix would be a commission reserve in the Router's trim headroom, which is your call since it's the gate.

- ✅ **`qc-rank` ranked a tutorial template #1 — FIXED 2026-09-18.** `rank_qc_library` skips runtime-errored backtests and those with fewer than `--min-orders` (default 20) QC "Total Orders", maps Sortino from QC stats, and defaults to `sortino_over_dd` (was Sharpe-based, against the standing ranking rule), as does the `qc-library` merge. Min-*duration* is not filtered: the QC backtest fields for it are unverified. 3 tests.

## Queue — build / run

- **Futures proxies for the chip + Japan equities — MEASURED 2026-09-25/26, verdict: a hedge, not a
  substitute (except for the Japan ETF sleeve).** User asked what futures could proxy the names just
  added. Two structural facts first:
    * **Single-stock futures do not exist** for NVDA/AVGO/MU/etc. — US single-stock futures died with
      OneChicago in 2020. A "proxy future" can only be an index future.
    * **`config/futures_universe.json` has no equity index future at all**: 23 contracts, every one a
      commodity (/CL /GC /HG /NG /SI /ZC /ZS /ZW, ICE softs, OSE platinum + rubber, SGX iron ore +
      rubber, canola). Adding an index future is a config addition, not a code change — the roll,
      lots and `AssetRouter` paths are already wired for futures.

  **Measured fit** (daily returns, cached bars, `reports/futures_proxy_fit_2026-09-25.csv`; beta and
  R² vs the ETF that stands in for each future):

  | name | proxy future | beta | R² | unhedged variance | ann vol | residual vol |
  |---|---|---:|---:|---:|---:|---:|
  | FLJP | Nikkei/TOPIX (via EWJ) | 0.96 | **0.949** | 5% | 18.2% | 4.1% |
  | JPXN | Nikkei/TOPIX | 0.93 | **0.874** | 13% | 17.6% | 6.2% |
  | SMH | /NQ or /MNQ | 1.40 | 0.781 | 22% | 35.7% | 16.7% |
  | SOXX | /NQ or /MNQ | 1.44 | 0.758 | 24% | 37.4% | 18.4% |
  | NVDA | /NQ | 1.74 | 0.612 | 39% | 50.3% | 31.3% |
  | AMAT / ADI / AVGO | /NQ | 1.06-1.50 | 0.51-0.53 | ~47-50% | 33-47% | 23-32% |
  | QCOM / NXPI / TXN / ON / MU | /NQ | 0.94-1.54 | 0.40-0.48 | 52-61% | 33-54% | 25-42% |
  | **SONY** | Nikkei (via EWJ) | 0.98 | **0.359** | 64% | 28.8% | 23.1% |
  | **INTC** | /NQ | 1.21 | **0.280** | 72% | 51.9% | 44.0% |

  **What follows**
    1. **The Japan ETF sleeve is genuinely replaceable by one future.** FLJP R² 0.95 and JPXN 0.87
       against a Nikkei/TOPIX proxy, residual vol 4-6%. One contract would replace three correlated
       ETF positions (EWJ, FLJP, JPXN are the same exposure three times — worth noting they are
       currently three of the 28 watchlist names competing for 6 slots), trade ~23h so it covers the
       after-US-close gap the exchange-hopping item is about, and OSE.JPN is **already a configured
       venue** (the platinum and rubber contracts trade there), so the plumbing exists.
    2. **No future proxies a single chipmaker.** /NQ leaves 39-72% of variance unhedged; INTC at 0.28
       and SONY at 0.36 are barely related to their index. Use /MNQ as a **beta hedge on the semi
       basket** (SMH/SOXX at R² ~0.78 are the only single tickers a future tracks reasonably), never
       as a stand-in for a named position.
    3. **Sizing is the constraint, as the existing config shows** — most of the 23 contracts are
       `enabled: false` precisely because `notional_per_contract` does not fit the position cap on a
       $100k book. Micros are the only viable rung: /MNQ ($2 × NDX), /MES ($5 × SPX), OSE Nikkei 225
       **micro** (¥10 × index) and mini (¥100 × index). Compute each against the vol-scaled cap and
       the 50% clamp before enabling anything.

  **Open work if this is taken up:** (a) probe whether OSE **index** futures return data on the paper
  login — index futures data is a different subscription from the missing TSEJ *equity* permission
  (error 162), and all futures fronts already resolved in the 09-21 probe, so this may be free;
  (b) `execution/venue_costs.py` has no futures entry — futures charge per contract, not per share,
  so the cost floor cannot price them yet; (c) currency: /NKD is USD-denominated while OSE Nikkei is
  JPY, which interacts with the numeraire question in the exchange-hopping item; (d) decide whether a
  future *replaces* the Japan ETFs (fewer, better-correlated positions) or *hedges* the semi basket —
  they are different mandates and the second one needs a short leg, which nothing in the book does today.

- **Japanese tech: searched 2026-09-25, NOTHING ADOPTED.** User asked for Japanese-tech exposure.
  Result: **no candidate clears the walk-forward gate, and pure Japanese tech is not reachable
  through Questrade at all.**
    * **Reachable (validated against the live Questrade symbol API):** SONY, EWJ, DXJ, BBJP, FLJP,
      JPXN, SCJ, HEWJ, DFJ — 9 of 14 candidates. `CAJ` (Canon ADR) is **not carried**, and
      `6758.T`, `8035.T`, `6861.T`, `9984.T` (Sony, Tokyo Electron, Keyence, SoftBank on their home
      exchange) all return "Symbol not found" — Questrade has no Tokyo listings, as expected.
    * **So the only single-name Japanese tech available here is SONY.** Everything else on the list
      is a broad Japan fund where tech is a sector weight, not the exposure. Real Japanese tech
      (Tokyo Electron, Advantest, Keyence, Disco, Renesas, Murata, Fanuc) needs the **.T listings via
      IB**, which are blocked on the missing TSEJ market-data permission — error 162 on daily bars,
      the one venue where even history fails. That makes it a dependency of the IB item, not a
      Questrade one.
    * **Walk-forward (5y cached bars, same harness as the equity pool):
      `reports/wf_symbols_japan_tech_2026-09-25.md` — 0 robust of 9.** Best OOS scores came with
      almost no trades: FLJP 7.73 (3 trades), BBJP 4.90 (2), JPXN 4.66 (3), EWJ 3.14 (2),
      SONY 2.46 (**10 trades**, the only one near the trade minimum) — every WFE between −0.02 and
      0.40, all under the 0.5 gate. DXJ and HEWJ produced 0-1 trades.
    * **Reading:** these names rarely trigger the book's strategies, so the scores rest on 1-3
      trades and mean nothing. Do not add any of them on this evidence.
    * **What would change the answer:** (a) deeper history — the cache warm pulled 5y (1,255 bars),
      enough for only ~4-5 folds, so a 10y pull is the cheap next test; (b) the Tokyo data
      permission, which unlocks the actual tech names rather than proxies; (c) a strategy family
      suited to these names, since the current set is what failed to trigger.

- **SCOPE (user direction 2026-09-25): unlock strategy capacity, walk-forward the currency
  sleeves, and hop exchanges after the US close.** Three separable programmes; A is cheap and
  self-contained, B reopens two closed decisions, C is gated on IB market data. None started.

  ### A. Unlocking strategy capacity — the bottleneck is slots, not strategies
  **Where it stands:** 44 strategies in `STRATEGIES`; the live QT map uses 7 of them and the crypto
  sleeve 5. `WALK_FORWARD_VALIDATED` holds 32 names, **all equity (29) + commodity (3)** — no FX, no
  crypto. Since 2026-09-24 the book runs `--params wf`, so the machinery to run registry configs
  in production exists and is exercised.
  **The trap:** 44 strategies x 32 names is 1,408 cells. Sweeping that and keeping the winners is
  how a repo manufactures overfit. And more validated strategies do **not** buy more exposure: the
  book has 6 position slots, so extra candidates only change *which* name wins a slot. Treat this
  as improving the ranking, not widening the book.
  **Steps:** (1) inventory which of the 44 have ever been walk-forwarded at all — the `candle_*`
  single-pattern family (17 of the 44) almost certainly has not; (2) pinned-parameter WF per
  strategy-family cell (the `scripts/calibration_sweep.py` protocol, not the re-optimising sweep),
  a-priori grids, config count recorded; (3) promotion only on WFE >= 1.0, OOS trades >= the class
  minimum, beating the incumbent by more than the fold-to-fold spread, still ahead at 2x costs;
  (4) then ask whether the winner displaces an incumbent in the strategy map, symbol by symbol.
  **Score on `sortino_over_dd`** with Sortino, return and time in market beside it.

  ### B. Walk-forward validating the currency sleeves — reopens two closed decisions
  **Say this out loud before spending time:** the guardrails record **"FX pair-trading: not
  tradeable here (2026-09-04)"** (cost drag 25-75% of the daily FX excursion; revisit only with a
  sub-pip cost model or `KalmanPairs`) and **"FX single-name sleeve: dropped (2026-09-05)"** (0 fills
  in 14 polls), plus **IB FX off-policy (2026-09-14)**. The user has now asked to revisit, so this is
  prompted — but the evidence that closed it has not changed, and nothing here should be presented as
  new until a cost model contradicts it.
  **What already exists:** `WF_PROTOCOLS["fx"]` (504 train / 126 test / 126 step, annualisation 260,
  `min_wfe` 0.5, `min_oos_trades` 10) with `data_source="pair-price feed (not yet wired)"`. Its own
  note says currency-*hedged ETFs* clear under the **equity** protocol. On disk: `AUDUSD_daily.parquet`
  (2,272 bars, 2020-06 -> 2026-09) and its trades file — one pair, deep; the other majors are not
  cached. `scripts/single_fx_wf.py`, `fx_pairs_scan.py`, `walk_forward_pairs.py` are written.
  **Two paths, and the cheap one is policy-compatible:**
    1. **Currency exposure through ETFs** (FXE / FXB / FXA / UUP / CEW.TO), walk-forwarded under the
       **equity** protocol: Questrade-fetchable, no new vendor, no FX venue, no sub-pip claim, and it
       reuses the whole existing pipeline. The dollar-hedge overlay already trades UUP, so the
       plumbing is proven. **Do this first.**
    2. **Native FX pairs**: needs a pair-price feed AND a cost model with a real spread per pair
       before any result means anything. `execution/venue_costs.py` (new 2026-09-25) is where that
       spread belongs — an FX entry with a sub-pip half-spread, not a flat fee. Until then a native
       FX walk-forward would be scored against a cost model that does not describe FX.
  **Acceptance:** a currency sleeve must clear the same gate as an equity name on `sortino_over_dd`
  net of **2x** costs, and must beat the trivial alternative of simply running the hedge overlay.

  ### C. Exchange shifting after the US close — the clock first
  **Correction to the framing:** London is not an after-4pm venue. Measured from `venues.py` for
  2026-09-25 (America/New_York):

  | venue | local session | NY open -> close |
  |---|---|---|
  | ASX (AUD) | 10:00-16:00 Sydney | **20:00 -> 02:00** |
  | TSEJ (JPY) | 09:00-15:30 Tokyo | **20:00 -> 02:30** |
  | SEHK (HKD) | 09:30-16:00 Hong Kong | **21:30 -> 04:00** |
  | LSE (GBP) | 08:00-16:30 London | **03:00 -> 11:30** |
  | US / TSX | 09:30-16:00 | 09:30 -> 16:00 |

  So the handoff after 16:00 NY is **Asia first (ASX + Tokyo, then Hong Kong), London pre-dawn**, and
  London's afternoon overlaps the US open rather than the US close. Any "follow the sun" scheduler
  should be built from `Venue.sessions` rather than from a hand-written clock, and must handle the
  DST drift between NY, London and Sydney (their offsets change on different dates).
  **Already built:** all five venues with sessions, currency, IB exchange and lot rules;
  `paper_global.py` (IB futures + overseas equities, one numeraire, `--equities`); `SessionRouter`
  queueing closed-venue intents; `priceMagnifier` pence handling and per-name board lots
  (fixed 2026-09-21, checked on real IB bars).
  **Blockers, in the order they bite:**
    1. **Live quotes: error 354 on every venue including SPY**, on both 2026-09-21 probes. Bars work
       everywhere except Tokyo. Re-probe after a *fresh paper login* first — it is free (option 1 of
       the four off-hours options).
    2. **Tokyo has no TSEJ market-data permission** — even daily bars fail (error 162).
    3. **LSE ETFs list on `LSEETF`** but `.L` routes to `LSE`, so ISF.L / VUSA.L do not resolve.
    4. **Numeraire and FX conversion**: a book spanning AUD/JPY/HKD/GBP needs one accounting
       currency and a conversion for equity, heat and cap math. `paper_global.py` takes a
       `--numeraire`; the Router's cap/leverage math is currency-naive and would need to agree.
    5. **Per-venue costs**: `venue_costs.py` models Questrade / Kraken / IB US. London adds **UK
       stamp duty (0.5% on purchases of shares; ETFs exempt)**, which dwarfs commission and must be
       in the model before any LSE result is scored.
  **Steps:** re-probe -> daily-bar dry run of the hop (plumbing evidence only, no P&L claim) ->
  decide subscriptions (US futures bundle before any Asia equity data) -> then **one venue at a
  time, easiest first: ASX, then SEHK (per-stock lots), then LSE (pence + LSEETF + stamp duty),
  Tokyo last (needs a data permission).**
  **Gate:** no overseas book trades until that venue's quotes, lots, currency conversion and cost
  model are each verified, and the whole path has been runtime-exercised on daily bars.

  **Cross-cutting:** none of C is reachable through Questrade, so it shares its fate with the IB
  equity-migration item below — the same feed swap, cost model and contract resolution serve both.

- **Dynamic portfolio holdings, managed every poll instead of only at entry (user direction
  2026-09-25, not started).** Today the book's weights are decided ONCE and then left alone:
  `weight_bias_for` is "computed ONCE at startup from the local candle cache" (`cli.py`), the
  per-name cap is evaluated only on a BUY, and `trim_to_slots` is a one-shot at launch. Between
  polls nothing re-weights: a name that drifts from 20% to 63% of the book keeps its shares until a
  strategy exit fires. That is what produced 2026-09-23's QT book (VDY.TO 63% of equity, −$557 of a
  −$533 gross day) — no gate was breached, the weights simply were never revisited.

  **What "real-time" should mean here.** The loop polls every 60-300s but the strategies read daily
  bars, so the target weights change slowly; what needs to be continuous is the *comparison* of held
  weights against target, plus a decision to act. Target weight per name from the existing pieces
  (OOS score + covariance bias, the vol-scaled cap as a ceiling, the intel overlay as a trim),
  recomputed each poll on live marks rather than at boot.

  **The precedent to copy, not invent:** `risk/hedge.rebalance_delta(current, target, band=0.20)` —
  a no-trade band as a fraction of the target position, so small drifts never trade. The dollar-hedge
  sleeve already rebalances this way inside `step()`; a holdings rebalancer is the same shape applied
  to the whole book, and it must route through `Router.submit` like everything else.

  **Dependency CLEARED 2026-09-24** (the partial-sell fix landed — see the ✅ entry in the
  correctness queue; 5 tests in `tests/test_paper_resume.py`). The argument is kept because it is the
  standing reason rebalancing needs correct accounting: a
  rebalance trims positions, i.e. it is a *partial* sell, and `PaperBroker._apply_fill` books
  realized P&L only on a full close. Rebalancing daily would push most of the book's P&L into the
  gap: equity would stay right, `realized_pnl` would stay 0.00, and per-session attribution would
  become unrepresentable. The emitter is fixed, so this no longer blocks — but verify per-session
  attribution against a live rebalancing session before trusting it.

  **Design constraints**
    * **Cost first.** At $4.95/fill a 5-name book rebalanced daily pays ~$50/day, which on $100k is
      ~12%/yr of drag. The no-trade band and the cost-aware floor (see the cost-aware sizing item)
      are what make this viable or not; pair them, and require each rebalance leg to clear the same
      round-trip-cost ratio a new entry must clear.
    * **Trim-only first.** Ship the reducing half (cut an oversized name) before the adding half:
      trims lower risk and cannot breach a cap, adds re-open every sizing question. An add is also
      an entry and belongs in the parallel allocator's slot/budget pass, not in a separate path.
    * **Never fight an exit.** If V1-V3 or a stop fires on a name, that wins; no rebalance may
      re-buy into a name the lockout is holding out of.
    * **Journal the target.** Write target weight, held weight, band and decision per symbol per
      poll (the `sizing_decisions.jsonl` shape), or the behaviour is unauditable after the fact.
    * **Resume-safe by construction:** rebalance fills land in the session's own journals, so
      `--resume-session` replays them; nothing extra needed.

  **How to judge it:** A/B two paper books on the same strategy map, rebalanced vs not, comparing
  `sortino_over_dd` net of the extra fills, realised max single-name weight, and turnover. The
  hypothesis is that capping concentration is worth more than the commissions it costs — which is
  exactly the thing 09-23 suggests but does not prove.

  *(Filed as the trading mechanism. If what you wanted was a live read-only holdings view instead,
  say so — that is a different, smaller item, and per the unfit-data principle it must show only
  closed-book-derived figures plus current marks.)*

- **Audit ledger + cryptographic continuity — PHASES 0–7 BUILT 2026-09-24, 8 scoped.** Full structure in
  `AUDIT_LEDGER_SCOPE.md`: an append-only event ledger (one row per state transition, hash-chained,
  signature persisted) plus an intent fingerprint shown on dashboard, device and ledger alike. Seven
  gaps found in the current stores; the worst is that **the card's signature is verified and then
  discarded** (`intents` has `signer_card_id`, no signature column), so no approval can be
  re-verified after the fact — **now fixed**, along with the missing correlation key and the
  partial-sell P&L bug (phases 0–2; see the scope doc's phase table for exactly what landed).
  Phase 3 landed too: `audit/ledger.py` (append-only, hash-chained per stream, fsync per row),
  Router dual-write behind an optional `ledger=`, `scripts/verify_ledger.py` (chain check + per-intent
  reconstruction, exit 1 on a break), on by default in `paper_kraken.py` via `--audit-ledger`.
  Phase 4 landed too: `audit/versioning.py` content-hashes the strategy (module source + that
  instance's params) and the risk gate (gate source + live thresholds); both are on every ledger row
  and shown by `verify_ledger.py --intent`. Known limit, documented: strategy versions do not follow
  imports, so an edit to `signals/indicators.py` changes behaviour without changing the version.
  Phase 5 landed too (mostly): STRATEGY_SIGNAL / SIGNAL_SUPPRESSED with reasons, RISK_TRIMMED,
  PARTIAL vs FILLED, BROKER_REJECTED, INTENT_QUEUED / RELEASED, INTENT_SENT, APPROVED / REJECTED /
  EXPIRED, SIGNED **with the signature in the row**, POSITION_FLATTENED. It found two real gaps
  (the card path's accepted gate emitted nothing; the Router compared fill quantity against a
  broker-mutated order) — both fixed. **Still unemitted:** INTENT_DISPLAYED (needs the store to
  record the card's first fetch), SIZED, KILL_SWITCH_TRIPPED, CARD_REGISTERED / CARD_REVOKED,
  FUTURES_ROLLED, CANCELLED.
  Phase 6 landed too, except firmware: the fingerprint is on `Prompt` and `PassbookEntry` (derived
  from the stored canonical bytes), in `PromptOut` / `PassbookEntryOut`, rendered by the PWA (pending
  prompts show HASH / ACCOUNT / STATUS; history shows the hash and signing card), and the card sim
  recomputes it and **refuses to sign on mismatch**. **Firmware is specified but unwritten** —
  `handle_prompt` in `firmware/tradecard/main/main.c` needs an mbedTLS SHA-256 over the same
  `canon_buf` and a display line; the 4-line LCD is full, so which line it replaces is your UX call,
  and it needs a device build I cannot do here.

### Cheap fixes cleared 2026-09-24 (working the backlog lowest-cost first)

* ✅ **Stale `state/STOP_KRAKEN_FETCH` removed.** It was left behind when the interleave driver was
  stopped by hand, and `kraken_interleave.py` exits between steps whenever it exists — so the 16:30
  scheduled fetch would have exited immediately every night without fetching anything. Nothing in the
  driver clears its own sentinel; worth deciding whether it should (a stop file that outlives the run
  it stopped is a foot-gun) — left as-is for now because an auto-clearing stop file can also lose an
  intentional "stay stopped".
* ✅ **`graph_journal.py --help` no longer crashes** on the cp1252 console. `sys.stdout.reconfigure`
  at import, as the paper runners already do, so callers no longer need `PYTHONIOENCODING=utf-8`.
* ✅ **`backtest/costs.py`: 4 invalid `\$` escapes fixed** — 8 SyntaxWarnings gone from every test
  run. No docs build renders those docstrings, so the backslash bought nothing.
* ✅ **`/v1/stats` stopped serving invented numbers as measured ones.** `overlay_scalar` was
  hardcoded `0.47`, `session_id` `"trading-session-1"` and `overlay_risk_zone` `"crypto"`. All three
  are now `null` and named in a new `placeholders` field, and the genuinely computed pending/expired
  counts — previously assigned and thrown away, which is what ruff's F841 was pointing at — are
  returned as `intents_pending` / `intents_expired`. `get_conviction_matrix`'s demo fallback is
  commented as such; **it still returns invented conviction when the allocator has no data**, so a
  client must not draw it unlabelled. Nothing outside the shim consumed these fields.
* ✅ **Ledger wired into `paper_ib.py` (stream `ib`) and `paper_global.py` (stream `global`).** All
  four runners now record: qt, kraken, ib, global — each its own chain, `--audit-ledger` on by
  default. The stream-separation test covers all four. This clears the phase 7 leftover, so
  `reconcile_ledger.py` will stop reporting IB/QT journal rows as missing once those books next run.
* ✅ **The BI endpoints stopped inventing data (2026-09-25).** `/v1/stats` and
  `/v1/conviction-matrix` each had **two** fabricating code paths — one in `ApprovalMetrics`, one
  inline in the route for when the shim has no journal, which is the path the tests actually
  exercised. Removed: a hard-coded 13x5 "conviction" grid **including SPY and BTC/USD, neither of
  which has ever been walk-forward validated**; a `0.5` "neutral conviction" for every unvalidated
  symbol; five per-strategy columns derived from one number by fixed offsets (`+0.15` because
  "momentum tends higher", `-0.20` for a "bearish overlay"); a `[0, 1]` clamp that hid that
  `oos_score` is an unbounded ratio running 19-111, so every real symbol saturated at 1.0; and
  `-0.2` drawdown / `4.2s` response / `0.47` overlay scalar in the no-journal stats branch.
  The matrix now reports only measured `(symbol, strategy)` pairs from `WALK_FORWARD_VALIDATED` —
  32 symbols, 7 strategies, **one value per row and null everywhere else**, because a null means
  "never validated", not "zero conviction". `metric` states the range so nothing renders it as 0-1.
  Three tests changed with it; each had been asserting the fabricated values, including one that
  pinned `starting_equity == 100_000` and one that required SPY to be present.
  **Still unfit for display:** per-strategy conviction is not computed anywhere in this codebase, so
  the heatmap is sparse by construction. If a real per-strategy conviction is wanted, that is a
  modelling task, not a rendering one.
* ✅ **45 unclosed-SQLite ResourceWarnings gone.** `SqliteCardRegistry` and `SqliteApprovalStore`
  held a connection for the life of the instance with no way to release it, so every short-lived one
  was reclaimed by the GC with its connection open — and the warning surfaced against whichever test
  happened to be running at collection time, which is why it looked like an arima/entries problem.
  Both now have `close()` (idempotent), context-manager support and a `__del__` backstop; the
  projection tests register their connections for teardown. Beyond the noise, an open handle on
  Windows keeps the `.db` locked against `tmp_path` cleanup. Suite-wide count is now **0**. One
  unclosed *file* warning remains in `tests/test_order_journal.py:43` (not Claude's file, left alone).
* ✅ **`execution/router.py` is clean under `mypy --strict`** — 18 errors to 0, on the file every
  order passes through. The 15 `arg-type` errors were one root cause: `build_default` assembled the
  gate knobs as a bare `dict` and splatted it into `confirm_live` / `confirm_autonomous` / the
  constructor, so mypy inferred a union value type and could not check a single argument — a
  misspelled key or a float where an int belongs would have reached the risk gate unchallenged. Now a
  `GateKwargs` TypedDict. The bare `list[dict]` annotations became `list[dict[str, object]]`, except
  `check_forced_exits(positions=...)` which is `list[dict[str, Any]]` on purpose: those rows come
  from a broker payload or a journal replay, mix strings with numbers, and the body reads them
  defensively, so claiming a precise shape would be a lie about data it deliberately distrusts.
  **Repo-wide mypy is 116 errors in 21 files**, so this is one file down, not the suite.
* ✅ **`scripts/build_desk_dash.py`** — "repopulate the dash" is now one command. It reads `state/`,
  computes every figure the panel draws (session ranking, curve, return distribution, round trips,
  attribution, activity heatmap, venue mix, gate denials, per-strategy acceptance, approval
  analytics) and injects one JSON blob into `pwa/desk.template.html`. It also prints the thin-sample
  warning: 7 closed trades against a 30-trade floor.
* ✅ **The Meter Atlas is folded into the desk panel** (2026-09-28) — the two pages in
  `OS-InvestmentIntelligenceDashboard/` are now one. The atlas is a fourth tab, ATLAS, restyled to
  the desk's dark tokens and unchanged in substance: 120 defined meters in 12 groups, neutral and
  needleless where nothing has been measured, a snapshot loader that rejects a file whole if any
  reading lacks a source, a scope or a plausible timestamp, search, and a per-meter detail dialog
  with the definition, unit, scale, code support and pinned source. The catalogue is baked in from
  `meter_catalog.csv`, the HISTORICAL snapshot is derived from the first row of
  `reports/calibration_sweep.csv` (all six readings or none), and the provenance line resolves the
  pinned commit through git rather than asserting it. One build writes both
  `pwa/desk.html` and `OS-InvestmentIntelligenceDashboard/TradeCard Desk Panel.html`, byte-identical.
  **Runtime-exercised**, not just built: loaded in a browser, the atlas renders 120 tiles, the
  historical snapshot reproduces the standalone page's six values exactly, a snapshot from
  `export_meters.py` run against the real journals loads and shows STALE/OBSERVED correctly, and a
  file with a future timestamp is rejected whole.
* ✅ **The desk panel is wired to a running session on both QT and Kraken** (2026-09-28) — the card
  shim now serves the panel, which is what makes it live: `GET /desk` is a public unlock shell that
  carries no journal data and never the token, and `GET /v1/desk/page` serves the built page behind
  the same auth as every other `/v1` route. Same-origin, so no CORS hole is opened and no token
  travels in a URL. The page polls `/v1/intents/pending` and `/v1/stats` and is **read-only by
  construction**: it shows the prompt and its fingerprint to compare against the card's screen, and
  the ACCEPT that decides an order is still a signature made on the device.
  * Kraken: `scripts/paper_kraken.py --require-card` prints the desk URL; new `--card-shim-host`.
  * QT: `cli signal --paper --require-card` gained the whole card path (`--card-shim-host`,
    `--card-shim-port`, `--card-ttl`) — it had none before.
  * **Runtime-exercised end to end**: with a shim booted against this repo's Router and one intent
    published, the panel showed `link live · 1 pending`, the prompt took the screen with the
    fingerprint the shim published (`A8AA...DC69`), and the TTL counted down. 11 new tests in
    `tests/test_desk_link.py`; the 119 approval-related tests still pass.
* ⚠️ **Three fabricated figures removed from the panel and its source** (2026-09-28), found while
  wiring the live link. (1) The AWAITING APPROVAL queue was a hard-coded pair of specimen rows
  (PAXG/USD, XIC.TO) with invented fingerprints; it is now the live queue, or an explicit "no link"
  note. (2) The takeover prompt was the same specimen; it is now either the live prompt or a
  **replay** of the newest real row in `approval.db`, ribboned as such, with the verdict that was
  actually recorded in place of the buttons. (3) `ApprovalMetrics.get_avg_ttl_response()` returned
  the literal **4.2** in every case — the loop meant to compute it was a `pass`, and the passbook
  did not carry `issued_at` anyway. `PassbookEntry.issued_at` is now populated by both stores, the
  median is measured over decided prompts only, and `StatsBody.avg_ttl_response` is nullable and
  named in `placeholders` when nothing has been decided. The header chips (kill-switch, card) are
  read from `state/HALTED` and the registered cards instead of being asserted in the markup.
* ⚠️ **Two defects the artifact wrapper had been masking** — found while folding, both in the
  already-committed `pwa/desk.html`. (1) `heat()` wrote to `#heat-n` / `#heat`, which existed in no
  markup, so the inline script threw there and **every statement after it never ran**: tab
  switching, the table toggles and the prompt takeover were all dead in the standalone file. The
  missing WHEN THE BOOK TRADES section is now in the BOOK panel. (2) `.takeover` sets `display:
  grid`, which outranks the UA rule for `[hidden]`, so the prompt overlay could be opened but never
  closed; the page now carries its own `[hidden] { display: none !important }`. Both only showed up
  outside the artifact host, which patches `[hidden]` itself.

### Phase 7 — SQLite projection + reconciliation — ✅ DONE 2026-09-24

Built as specified: `audit/projection.py`, `scripts/rebuild_projection.py`,
`scripts/reconcile_ledger.py`, 20 tests. The ledger stays the write path and the source of truth;
the projection is **derived and disposable**, rebuilt from scratch by replaying
`state/ledger/*.jsonl`. If the two disagree, the ledger wins and the disagreement is the finding.

**Verified on a seeded ledger** (11 events → 3 intents): outcomes folded correctly, a tampered row
reported as `chain_status.ok = 0` instead of being projected as sound, and 2 journal fills plus 1
rejection reconciled exactly against `fills.jsonl` / `rejected.jsonl`. **Against the real
`state/ledger/` it reports 0 events** — no session has yet traded with the ledger on, so there is
nothing to project. That is the honest state, not a failure.

**Remaining from this phase:** wire `ledger=` into the other runners (`cli.py signal`,
`paper_ib.py`, `paper_global.py`) — the same three lines as `paper_kraken.py`. Until then only the
Kraken book records, and `reconcile_ledger.py` will keep reporting QT/IB journal rows as missing on
the ledger side. Retire no journal until reconciliation is clean for a full session.

Original spec kept below for the parts not yet done.

* **Tables** (all `PRIMARY KEY`-ed on ledger identity so a replay is idempotent):
  `ledger_events(stream, seq, ts, event, intent_id, …, row_hash)` — the flat spine;
  `intents(intent_id, first_seen, last_event, symbol, action, shares, strategy_id,
  strategy_version, risk_check_version, verdict, signer_card_id, fingerprint, broker_order_id,
  filled_qty, outcome)` — one row per intent, folded from its events;
  `chain_status(stream, rows, last_seq, last_row_hash, verified_at, ok, reason)` — so a verification
  run is queryable rather than only printed.
* **Queries it must answer** (scope §2.5, currently only answerable by grepping JSONL):
  rejection rates per gate reason per week; time from `INTENT_SENT` to `APPROVED` (the TTL-pressure
  question the card UX needs); which `strategy_version` traded a symbol on a date; every intent with
  a `SIGNED` row whose signature no longer verifies; intents that reached `BROKER_SUBMITTED` but have
  no terminal event (the crashed-writer case).
* **Reconciliation** (`scripts/reconcile_ledger.py`, exit non-zero on mismatch): ledger `FILLED` /
  `PARTIAL` rows against `state/fills.jsonl` and `paper_fills.jsonl` by `(intent_id, order_id)`;
  ledger `RISK_REJECTED` against `rejected.jsonl`; `approval.db` verdicts against ledger
  `APPROVED` / `REJECTED` / `EXPIRED`; replayed positions/cash against the session's last
  `paper_equity.csv` row. **Expect real mismatches on the first run** and treat them as findings, not
  bugs to paper over: sessions started before 2026-09-24 have no ledger at all, and the Kraken book
  is the only runner wired (`--audit-ledger`), so QT and IB sessions will have journal rows with no
  ledger counterpart until they are wired too (small: pass `ledger=` in `cli.py signal` and
  `paper_ib.py` / `paper_global.py`, same three lines as `paper_kraken.py`).
* **Retire nothing yet.** The existing journals stay until reconciliation is clean for a full
  session; only then is dropping a writer a discussion.
* **Watch:** the projection must never be on the trading path — build it in a separate process, not
  inside the poll loop.

### Phase 8 — daily anchor + retention + trusted time (BLOCKED on three decisions)

Mechanics are easy; both open questions are about trust, so they are yours.

* **What it does:** at UTC rollover, for each stream, compute the Merkle root of that day's rows,
  sign the root with a key the trading process cannot use, and append
  `state/ledger/anchors/<date>.<stream>.json` (`{date, stream, first_seq, last_seq, row_count,
  merkle_root, signature, key_id}`). Publish the root somewhere append-only. That bounds tampering
  to the current day: a rewrite of any earlier row changes the root, and the root is already signed
  and copied elsewhere.
* **🔴 DECISION 1 — where the anchoring key lives.** An anchor signed by a key sitting beside the
  ledger, usable by the process that wrote it, proves nothing about that process. Options, cheapest
  first: (a) a second Ed25519 key in `state/` — honest about being weak, only defends against an
  edit that doesn't think to re-sign; (b) **the TradeCard signs the daily root** — the hardware is
  already there, already holds a key the host cannot read, and this is the option that actually fits
  the design; costs a daily tap; (c) an offline key on removable media, signed weekly rather than
  daily; (d) publish the root to a remote append-only place and treat public visibility as the
  anchor. Recommendation: **(b), with (d) as the publication channel** — it matches the
  zero-vendor-infrastructure posture where GitHub is the coordinator, so a commit of the day's root
  to a private repo is both the copy and the timestamp, with no service to run.
* **🔴 DECISION 2 — retention and WORM.** Today nothing stops a process — including the test
  suite, which has done it twice — from writing into `state/`. Needs: a retention period (how long
  ledger files and anchors are kept, and whether anything may ever be deleted); where the off-box
  copy goes and how often; whether to set the Windows read-only attribute on closed day files (cheap,
  defeats an accident, not an attacker); and whether the ledger directory should be moved out of
  `state/` so the "everything under state/ is fair game" convention stops applying to it.
* **🔴 DECISION 3 — trusted time (surveyed 2026-09-24, see scope §2.6).** We have timestamps; none
  of them is evidence. `ts` on every ledger row, and every `issued_at` / `resolved_at` in
  `approval.db`, is the writing host's own clock. `seq` + the chain carry **order** but say nothing
  about wall-clock. The card has no clock at all, by design: `prompt_ttl_seconds` takes the window
  from the server's `issued_at`/`expires_at` so no NTP is needed, and its passbook stores uptime
  since boot — so the card can contradict the host about the bytes, never about the time. Set the
  system clock back and every new row follows it while the chain still verifies.
  **One property worth not losing:** `mint_intent_id()` is `f"{time.time_ns():016x}-…"` and
  `intent_id` is inside the signed canonical bytes, so an approval cannot be re-dated without
  invalidating its signature (`18d84e7c8730f1e0` → `2026-09-24T16:25:48.630815+00:00`). It is still
  only the host's claim, signed by the card — and anyone reformatting the id would delete this
  silently. Options: (a) log `time.monotonic()` beside `ts` so an in-session clock jump is visible —
  cheap, catches accidents only; (b) publish the daily root remotely and let the publication time be
  a timestamp we did not author; (c) an **RFC 3161** timestamp token over the daily Merkle root —
  one HTTP call, a standard token, verifiable by any third party, no service of ours to run.
  Recommendation: **(c) plus (b)** — together they make the anchor mean "this existed by then"
  instead of "we say this existed by then". Note this is a prerequisite for the anchor being worth
  much: a root signed locally records a time the same host chose.
* **Do not claim immutability before this lands.** The chain makes edits *detectable*; the anchor
  makes them detectable *by someone else*; only WORM storage makes them *impossible*. The scope doc
  and `audit/ledger.py`'s docstring both say so — keep it that way in any write-up.
* **Also still open from phase 5–6:** `INTENT_DISPLAYED` (the store must record the card's first
  fetch of a prompt — a small `approval.db` migration plus a touch in `pending()`), and the firmware
  fingerprint line.
* ✅ **`INTENT_CREATED` now has an emitter — FIXED 2026-09-25, taxonomy coverage 25/25.** Neither of
  the two options in the original finding was needed: rather than stamping N construction sites or
  deleting the event, `Router.announce_intent()` writes it the first time a router sees an intent,
  carrying the intent's own `timestamp` plus `pre_gate_ms` — so the gap between "the strategy built
  it" and "the gate ran" is on the record without the Router having to be where it was created.
  Announced once per intent via a bounded `deque`: **the card path gates, waits for a verdict, then
  submits the same intent again**, so a naive emit in `submit()` put the creation event in the middle
  of the chain after SIGNED, reading as a second intent. `ApprovalRouter` calls `announce_intent`
  before its own pre-prompt gate; the inner router's call is then a no-op. 3 tests, and the
  diagnostic harness now demands full coverage rather than tolerating one gap.
* **Display target changed (user, 2026-09-24): ideally an 8" 1280x800 touch panel** for
  firmware/dash-type devops. This obsoletes the phase 6 firmware note that asked which of the 6 SPI
  LCD lines the hash should replace — at 1280x800 there is no line-budget problem, and the
  fingerprint, thesis, device id and status all fit at once. Two consequences worth thinking through
  before writing any firmware: (a) the current `firmware/tradecard/main/main.c` drives a small
  monochrome SPI panel with a 5x7 font (`lcd_puts`, `LCD_COLS`, `font5x7.h`) — a 1280x800 touch panel
  is a different device class (RGB/MIPI + touch controller, likely a different SoC or an SBC), so this
  is a port, not a display tweak; (b) at that size the **PWA is the natural dashboard** — it already
  renders the hash, account and status — so the question becomes whether the card stays a minimal
  tap-to-approve device with the 8" panel as a separate dash, or whether one touch device does both.
  The security property does not change either way: whatever displays the intent must show the
  fingerprint derived from the same record the signer received. The PWA is currently styled for phone
  width, so it would want a wider layout for an 8" panel.

- 🟡 **Cost-aware minimum position size — BUILT 2026-09-25, off by default, not runtime-exercised.**
  `execution/venue_costs.py`: `VenueCostModel.for_venue()` prices one side per venue (Questrade flat
  $4.95; Kraken 26 bps taker; IB $0.005/share, $1 order minimum, capped at 1% of notional; anything
  unrecognised or duck-typed gets the conservative flat fee), plus `round_trip_cost_ratio` and
  `min_notional_for`. Wired into `Router` as gate 11 (`max_round_trip_cost_ratio`, entries only,
  evaluated AFTER the size-cap trim so it judges what would actually be sent; the rejection names the
  ratio, the venue and the minimum ticket) and into `PaperBroker`, whose commission is now
  venue-priced unless a caller pins one. 13 tests in `tests/test_venue_costs.py`; suite 1415 passed,
  1 skipped. Flags: `--max-cost-ratio` on `cli signal` and `paper_kraken.py`, default 0.0 = off.
    * **Finding that shapes the setting:** Kraken's taker fee alone is ~52 bps round trip, so a
      0.5% ceiling fits at NO size there, while the same ceiling implies a ~$2,475 minimum ticket on
      Questrade and ~$500-800 on IB. **The ceiling must be per-venue** (or ~2% on Kraken). A
      percentage-fee venue needs no size floor at all — its ratio is size-independent.
    * **Still open:** pick the ceilings from data (replay every closed round trip: cost-as-%-of-
      notional vs realised return, see what 0.25/0.5/1% would have refused and what those netted);
      decide whether `min_ticket_usd` stays as a floor underneath; add an FX spread entry when the
      currency-sleeve work needs one; the backtest engine's `CostModel` is still separate, so
      backtests and the live gate do not yet share one cost source.

- **(original scope) Cost-aware minimum position size, per venue and per share count (user direction 2026-09-23).** The min-ticket gate is a flat `min_ticket_usd: 100.0` in `trading.yaml`, applied
  identically on every venue. It is blind to what a fill actually costs, so the engine happily opens
  positions that cannot pay for themselves. The rule the user asked for: **a position is only worth
  taking when its size is large relative to the commissions it will pay — and what "large" means
  depends on the share count and the venue.**

  **Evidence (Kraken paper session `bc361280`, 2026-09-23).** One fill all session: LINK/USD 14.55
  units, **$184.97 notional, $4.95 commission = 2.68% of notional on entry, ~5.4% round trip.** It
  cleared the $100 floor with room to spare and would have needed a 5.4% move to break even. At a
  flat $4.95/fill, **anything under roughly $2,000 is structurally unprofitable**, i.e. the floor
  permits positions 20x too small.

  **Two defects, and they compound**
    1. **The floor is not cost-aware.** `Router._gate` compares notional to a constant. It should
       compare the *round-trip cost fraction* to a ceiling: reject when
       `round_trip_cost / notional > max_cost_ratio` (0.5% is a defensible starting point, which at
       $4.95/fill implies a ~$2,000 minimum). `signals/profit_lock.round_trip_cost_frac` already
       computes exactly this number for the exit side, and `backtest/costs.CostModel.per_side_frac`
       does it for backtests — the gate is the only place that doesn't use it.
    2. **The commission model is not venue-aware.** `PaperBroker(commission_per_trade=4.95)` is
       Questrade's equity commission, and `scripts/paper_kraken.py` uses that default unchanged, so
       **the crypto book is charged $4.95 per fill when Kraken actually charges ~0.16-0.26% of
       notional** (~$0.40 on that LINK trade). The flat model overstates small crypto trades and
       would understate large ones. IB is different again: per-share with a per-order minimum and a
       cap as a % of trade value, so **cost per share falls with price and rises with share count** —
       which is precisely the "depends on share-size and venue" the user named.

  **Shape of the fix**
    * One `VenueCostModel` (extend `backtest/costs.CostModel`) resolved per venue: Questrade flat
      per fill, Kraken bps of notional, IB per-share with min/max. One source of truth for
      `PaperBroker`, the Router's min-ticket gate, the profit lock's arming test and the backtest
      engine — today three of those four disagree.
    * Gate becomes `max_round_trip_cost_ratio` (keep `min_ticket_usd` as a floor underneath it).
      **Reject, never size up**: if the risk-sized position is too small to carry its costs, the
      trade is not worth taking — inflating it past the sizer's answer would be the tail wagging
      the dog.
    * The rejection reason must name the number ("round-trip cost 5.4% of notional > 0.5% cap"),
      because a silent skip looks like a missing signal in the journals.
    * Venue tags already exist on every journal row (`venue` on fills/orders), so a replay can
      attribute costs correctly once the model is venue-aware.

  **Measure before choosing the ceiling.** Replay `state/paper_fills.jsonl`: for every round trip
  ever closed, compute cost-as-%-of-notional and the realised return, then ask what a 0.25% / 0.5% /
  1% ceiling would have refused and what those refused trades actually netted. Pick the ceiling
  from that distribution rather than from the $4.95 arithmetic alone — and note the obvious
  confound, that the flat-$4.95 model has mispriced every crypto fill in the journal to date.

- **FIRST STEP of the IB equity move: diff the two feeds' daily bars (2026-09-23, not started).**
  Standalone and runnable now — it is step 2 of the IB item below, pulled out because it blocks
  everything else and needs no decision. Every walk-forward parameter, every backtest and every
  calibration in this repo was fitted on **Questrade** bars. If IB's history differs materially,
  those numbers do not transfer and the migration is not a feed swap but a re-fit.

  **What to build:** `scripts/feed_diff.py --symbols <list> --years N`, writing
  `reports/feed_diff_<date>.{csv,md}`.

  **Data sources, and why this can run while a QT session is live:**
    * **Questrade side: from the local cache only.** `data/cache/` already holds 18,327 shards
      across 590 symbols (`EQB_TO_1d_<hash>.parquet` — dots become underscores, which is why a
      naive `EQB.TO_1d_*` glob finds nothing). No token, no API call, **so no risk to the one-shot
      refresh token of a running QT session.** The 2026-09-22 defense-name gap has since closed
      itself: ITA/NOC/GD now have shards, fetched by the live session.
    * **IB side: `IBBroker.candles(..., whatToShow="TRADES", useRTH=True)`** with TWS on 7497.
      Daily bars worked on every venue except Tokyo in the 09-21 probe, so **this step does not
      need the missing live-quote subscriptions**.

  **What to measure**, per symbol, over the overlapping window:
    1. Coverage: bar counts, first/last date, missing sessions on each side (calendar mismatches
       are as damaging as price differences).
    2. Close-to-close: mean and max absolute difference in bps, and the count of bars over 10 bps.
       Split TSX (`.TO`) from US — currency, listing venue and consolidated-vs-primary tape all
       differ, and TSX is where a surprise is most likely.
    3. Return series: correlation and, more usefully, the distribution of daily return differences.
       A constant price offset is harmless to a strategy; differing returns are not.
    4. OHLC beyond the close: the ATR stop and the candle/overbought exits read high/low/open, so a
       feed that agrees on closes can still move every stop. Check ranges, not just closes.
    5. Adjustment convention: pick a few names with recent dividends or splits (ENB.TO, VDY.TO,
       SLF.TO all pay) and check whether the two feeds adjust the same way — this is the most
       likely source of a systematic gap.

  **Acceptance, decided before looking at the output:** if median close differences are within a
  few bps and the return distributions match, the registry params carry over and the migration
  proceeds. If TSX or the adjustment convention differs materially, the honest conclusion is that
  **the walk-forward has to be re-run on IB bars** before any IB-fed book trades — record that as a
  cost of the migration rather than discovering it later in a paper A/B.

  **Then:** re-run one or two known backtests (a `ts_momentum` name and a `bollinger` name) on IB
  bars and compare `sortino_over_dd`, OOS return and trade count against the QT-fitted result.
  That converts a bar-level diff into the number that actually matters.

- **Move the equity book from Questrade to IB — US/TSX too, not just futures and options (user
  direction 2026-09-23, not started).** Today's split is a desk policy, not a technical limit: IB
  carries futures and overseas listings (`.L`, `.AX`, `.T`, `.HK`) and `scripts/paper_global.py`
  actively *refuses* US/TSX names (`assert_ib_no_questrade_equities`), while the whole QT path —
  `cli.py signal`, `backtest`, `paper`, `tune`, the candle cache and pre-flight symbol validation —
  is wired to `_make_questrade` at 8 call sites. The ask is to make IB the equity broker as well.

  **Why it is worth doing**
    * One broker for every asset class: one calendar, one contract model, one cost model, one
      account. `AssetRouter` already maps asset class -> brokerage; today equity is the odd one out.
    * **Questrade's refresh token is one-shot and process-local.** A second QT process invalidates
      the running session's token, which is why the defense names (ITA/NOC/GD) could not be
      cached on 2026-09-22 while a QT paper session was live. IB has no equivalent constraint.
    * Costs: QT is modelled at a flat $4.95/fill; IB tiered US equity pricing is per-share with a
      small minimum, materially cheaper for the sizes this book trades — and the paper cost model
      would finally match the venue it claims to simulate.
    * Limit orders, extended hours and real-time quotes all live on the IB side of the codebase
      already (`brokers/ib.py`, `brokers/ib_web.py`, both with `quote` / `candles` / `place_order`).

  **Blocking facts — read before starting**
    * **No live quotes yet.** Probe 2026-09-21 (paper login on 7497): error 354 on *every* venue
      including SPY, twice, before and after enabling real-time sharing. Daily bars work everywhere
      except Tokyo. Per "Held for market data", an IB-fed book without quote permissions launches
      and never trades. **However** — the strategies decide on completed DAILY bars; live quotes are
      used for fills, marks and the stop/profit-lock checks. So a daily-bar IB paper book is
      testable before subscriptions exist, which is the cheap first step below.
    * The `interactive-brokers` MCP server failed to connect again on 2026-09-23
      (`CONNECTION_CLOSED`), and TWS must be listening (7497 paper / 7496 live). Any IB path
      inherits that operational dependency; QT needs only a token.
    * `brokers/ib.py` is built on `ib_insync`, which is unmaintained — the `ib_async` migration is
      already queued in the dependency-caps item. Do that first or accept the debt knowingly.

  **Work, in order**
    1. **Feed swap behind a flag**, not a rewrite: `--feed {questrade,ib}` on `cli.py signal`
       (and `backtest`). `MarketData` takes any `Broker`, so the seam is `_make_questrade`. Keep QT
       working the whole time — this must be A/B-able, not a cutover.
    2. **Cache separation.** Do NOT mix QT and IB bars in the same parquet shards: different
       adjustment and timestamp conventions. Key the cache by source, and **diff the two feeds over
       their overlap** (close-to-close deltas per symbol) before trusting IB history. Every WF
       registry param and every backtest in the repo was fitted on QT bars.
    3. **Symbol resolution and pre-flight.** `.TO` -> TSE/SMART with CAD currency, US -> SMART;
       replace the QT-candles-404 validation with IB contract resolution + a one-bar fetch, so a
       bad symbol still refuses the launch. `stock_details()` price-magnifier handling already
       exists; US/TSX shouldn't need it, but assert rather than assume.
    4. **Cost model.** Replace the flat $4.95 in `CostModel` / `PaperBroker(commission_per_trade=)`
       with IB tiered (per-share, per-order minimum, max % of notional). Paper P&L, the profit
       lock's round-trip cost gate and the min-ticket gate all read it.
    5. **Daily-bar paper A/B (the cheap first run).** Same strategy map, same `--paper-equity`, one
       QT-fed book and one IB-fed book side by side, marks from the last completed bar where live
       quotes are unavailable. Compare fills, fill prices, costs and `sortino_over_dd`. Two books
       double the alert volume — mute one.
    6. **Live path parity** only after 1-5: order types, TIF, TSX routing, account base currency
       (CAD vs USD) and the FX leg for a mixed book, plus `AssetRouter` / approval-card venue tags
       (`approval_asgi.Broker` already knows `ib`, `ib_web`, `questrade`).
    7. **Decide QT's fate explicitly.** Keep it as a second opinion on quotes, or retire it? If it
       stays, the one-shot-token constraint stays with it and must be documented where it bites
       (concurrent processes).

  **Do not** flip the desk-policy refusal in `paper_global.py` as the first move — that guard is
  what keeps a half-migrated book from double-trading a name on two venues. It comes down in
  step 6, once one venue owns the equity book.

- **Wire the RMT eigen representation into the live analysis layers (user decision 2026-09-22,
  not started).** Question raised: is the eigenvalue representation built into the top layers of
  analysis? **No — it is research-only.** `analysis/rmt.py` (commit `9b9958f`: Marchenko-Pastur
  band with a Tracy-Widom margin, k-deep embedding, trace-preserving noise shapes) has exactly one
  importer in the repo, `scripts/rmt_lstm_study.py`. Nothing in the live path imports it. Meanwhile
  every correlation the live path does use is a **raw sample estimate**, which on a 5-name book is
  mostly noise:
    * `risk/risk_model.py` `portfolio_risk(method="corr")` — raw rho with Dimson +/-1 lead-lag,
      `sqrt(r' rho r)`, run every poll for the heat gate;
    * `risk/allocation.py` via `cli.py` — CVaR + score softmax, computed once at session start,
      multiplies entry conviction through `weight_bias_for`;
    * `risk/entry_allocation.py` (new 2026-09-22) — ranks slots by conviction only, no correlation;
    * `intel/interpret.py` — no correlation input at all.

  Three entry points, **ranked by cost/benefit — do them in this order**:

    1. **Denoise the heat gate's correlation matrix.** Swap the raw rho in
       `portfolio_risk(method="corr")` for the MP-filtered one. Cheapest: self-contained, no new
       data, one call site, and the study code already exists. Highest immediate value because the
       heat gate's diversification credit is currently computed from noise — a spuriously low
       off-diagonal lets the book carry more risk than intended. **It changes when the gate binds,
       so it changes sizing:** A/B the same sessions, `sortino_over_dd` primary with Sortino,
       return and time in market beside it, plus a count of how often the gate's verdict flips.
    2. **Eigen-aware entry ranking in the parallel allocator.** Today's slot ranking is conviction
       alone, so two names loading on the same dominant eigenvector can take both free slots. Rank
       (or trim) by conviction discounted by the candidate's loading on the top eigenvector of the
       denoised matrix, i.e. prefer the name that adds a new direction. This is the honest version
       of what `weight_bias_for` is reaching for, and it belongs in the allocator now that the
       whole poll is decided at once. Needs the eig computed per poll (cheap at 5-19 names) or
       cached per session like the current bias. Medium cost, medium evidence burden: it decides
       which names enter, so judge it on realised book correlation and `sortino_over_dd`, not on
       how sensible the ranking looks.
    3. **Top-eigenvalue share as an intel thesis input** ("cross-asset correlation spike"), the
       third of the new inputs queued in the thesis-layer item above. Computed locally from cached
       daily returns, no vendor, and it measures the thing that actually hurts a concentrated book:
       everything moving together. Lowest priority of the three because it feeds the layer that
       has never been scored (see step 4 of that item) — do it after the thesis layer earns its
       keep, or alongside, but do not let a new gate start trimming size before then.

  **Caveat for all three.** MP denoising has its own parameters and the commit message records the
  sigma2 estimate already misbehaving once (a fixed-point iteration collapsed 1 -> 10 signal modes
  on crypto). Pin the estimator and the lower cut, record them, and treat a denoised matrix as a
  change to position sizing that needs a before/after — not a drop-in improvement.

- **Intel thesis layer — fix the inputs and the gates before adding theses (user decision
  2026-09-22, not started).** Question raised: only 2 of the 8 theses fire consistently, so should
  we build more on top of them? Answer: not yet, and these four steps first. Evidence is in the
  `intel/interpret.py` module header. Four theses are gated on values the feed has never reached
  (`fear_greed >= 70`/`<= 25` against an observed 54-68, zero fires in 280 reads; disasters `>= 5`
  against a max of 2; `disaster_accel` whose key is absent from every payload; conflict/energy
  accel `>= 2.0` against a max of 1.20), and the two that do fire are near-constants since the
  09-18 gate change (Complacency 81%, Conflict watch 70% of real reads). A conjunction of two
  ~80% gates still fires ~65% of the time, so composing new theses out of the existing eight adds
  words, not information. **Ordered, 1 blocks 2 and 4.**

    1. **Degraded-row detection is wrong.** 89 of 336 rows in `state/intel_overlay.jsonl` carry no
       strategic-risk reading and no VIX yet are flagged `degraded: false`. Every base rate,
       calibration and backtest of this layer reads those rows. Fix where the flag is set
       (`intel/overlay.py` / `intel/worldmonitor.py`), and treat old rows at READ time — a
       `is_real_read(row)` helper used by every replay — rather than rewriting the journal
       (`state/` is ground truth).
    2. **Trailing-percentile gates instead of hard constants.** Already flagged as the queued
       design change in the module header ("a trailing-percentile gate would be regime-robust
       where a hard constant is not"). Replace `strategic_risk >= 67` / `conflict_events_active
       >= 4` (and the accel gates) with "this read is in the top decile of the last N real reads",
       N ~90 days, with a minimum sample before the gate arms and the current constants as the
       cold-start fallback. This is what actually fixes "only 2 fire consistently": every thesis
       then fires at a designed rate in any regime, and a WorldMonitor recentring of the index
       can't silently invalidate the constants. **Sizing impact: the same gates drive the
       live-loop conviction trim** (Complacency at moderate = x0.75 on the safe-haven exemplars),
       so a firing-rate change is a position-size change — measure before adopting.
    3. **New inputs, which is where genuinely new theses can come from.** The existing feed is one
       slow-moving geopolitical index plus event counts; no rewording of it will produce a
       signal it doesn't carry.
         * **VIX via IB** (`Index("VIX", "CBOE")`): coverage 77% -> ~100% and real-time. Note the
           header's caveat — this alone changes no firing rate, both `calm_market` legs were
           already satisfied.
         * **VIX term-structure inversion** (VIX vs VIX3M, or the front two VX futures): a real
           regime marker with actual range, unlike the strategic-risk index. Check the
           "Held for market data" section first — VX futures need an IB subscription; the
           VIX/VIX3M index pair may not.
         * **Credit spreads**: HYG vs LQD vs IEF as a Questrade-fetchable proxy (no new vendor, no
           subscription, already in the cache pipeline). Widening high-yield spreads against a
           calm VIX is the honest version of the complacency thesis the current index only gestures at.
         * **Cross-asset correlation spike**: computed locally from cached daily returns — the top
           eigenvalue's share of the correlation matrix, straight out of the RMT / MP-denoising
           work on this branch (`9b9958f`). No vendor at all, and it measures the thing that
           actually hurts a 5-name book: everything moving together.
       Each new input needs coverage stats and a percentile distribution recorded before any
       thesis is written on it, so step 2's gates have something to sit on.
    4. **Score the layer before expanding it.** Nothing has ever measured whether a thesis trim
       helped. Replay the corpus, tag each thesis ONSET (not each firing read — the de-duplicated
       alert stream is what acts), and compare forward returns of that thesis's `THEME_EXEMPLARS`
       against the no-thesis baseline over 1/5/20 days, net of costs, scored on `sortino_over_dd`
       with Sortino, return and time in market beside it. State the sample honestly: one regime,
       ~4 weeks, so this is a sanity check on sign and magnitude, not validation. If the trims
       show no edge, the answer is fewer theses and a smaller trim, not more theses.

- **Limit orders, so entries stop paying the full spread + slippage (added 2026-09-22, not
  started).** Today's paper cost breakdown: $214.36 of −$595.68 was "spread + slippage", which is
  the paper broker's flat model (mid + 5 bps per fill, on ~$429k traded). Limit orders are the real
  lever against that cost, but nothing in the path supports them yet:
    * `execution/router.py` builds every order as `OrderType.MARKET` (the `Order(...)` in `submit`).
    * `brokers/paper.py` `place_order` fills immediately at mid (or touch) ± `slippage_bps`;
      `order.limitPrice` is only a fallback reference price, and `limit_price` is always null in
      `paper_orders.jsonl`.
    * `brokers/models.py` `Order` already carries `limitPrice` and serializes it for Questrade.

  Scope when picked up:
    1. **Intent → order.** A limit price on `OrderIntent` (entries only at first): e.g. join the bid
       for a buy, or mid, with a time-to-live. Stops, flatten, slot trims and V1–V3 exits stay
       market, since those need to get out.
    2. **Paper simulation.** A limit order rests in a pending book and fills on a LATER poll only
       when the market trades through it (buy: ask ≤ limit), at the limit, with no 5 bps charge.
       Unfilled orders expire at TTL and are journaled as expired, never silently dropped. Model
       adverse selection honestly: a resting buy fills mostly when price is falling.
    3. **Risk gate.** The Router gates at submission; decide whether it re-gates at fill time (the
       book, heat and kill-switch may have changed while the order rested).
    4. **Cash and slots.** The parallel allocator and the position cap must count pending orders,
       or two resting orders can oversubscribe the same headroom.
    5. **Resume.** `--resume-session` must rebuild pending orders from the journal, or expire them
       explicitly on restart. Today resume replays fills only.
    6. **Live path.** Questrade limit order placement + status polling (partial fills, cancels).
       Paper first.

  **How to judge it:** fill cost vs the mid baseline per fill, **and** the cost of missed trades
  (signals that expired unfilled, marked to where the trade would have gone), on the same
  sessions. A limit policy that saves 5 bps but misses the winners is worse. Score with
  `sortino_over_dd` as usual.

- **Runtime exercise of `composite` + `confirm_*` — STARTED 2026-09-17.** The QT strategy map now
  runs `ENB.TO=confirm_bollinger`, `SRU.UN.TO=confirm_rsi_meanrevert`, `XIU.TO=composite`; the
  other 11 names are unchanged. Kraken is unchanged (crypto would need `symbol` passed into the
  confirmation filter so gap patterns are dropped). First session: `da6c798e…`. Run ≥1 week and
  compare fill count / holding period / return per fill against those names' earlier
  base-strategy sessions. The comparison is across different days, so regime is a confound.
  Not promotion evidence.
    * **Evidence so far — one 2.5 h session, far too little to judge anything.** `2c8274d6…`
      (2026-09-17, 85 polls, 60 s warm-up then 300 s): SRU.UN.TO `confirm_rsi_meanrevert` filled
      1,500 @ 26.6983 and closed at 26.7016 on the flatten → **+$4.95 gross, roughly flat**.
      `ENB.TO` (confirm_bollinger) and `XIU.TO` (composite) **never signalled**, so both still have
      zero runtime evidence. Session net +$29.25 after $39.60 of commissions, driven by VDY.TO
      (ts_momentum), not by the new strategies.
    * All 4 fills landed on poll 1 and every later poll was a hold, so intraday polling added no
      entries — consistent with daily-bar strategies.
    * `confirm_*` run their intended pinned params (30/3.0 and 14/35) regardless of the params bug.
      `composite`'s members run class defaults.
    * **Bug found and fixed 2026-09-17 (uncommitted):** `ConfirmOverlay` masked `signal_strength`
      by the event channel only, so every level-triggered confirmed entry sized to **0 shares** on
      the `--level` QT path. Observed on `da6c798e…`: SRU.UN.TO alerted ENTRY with 0 shares.
      Fixed in `strategies/overlay.py`; regression tests in `tests/test_overlay.py` (verified
      failing on the old code). Runtime-confirmed on `2c8274d6…` (2026-09-17 15:11 UTC):
      SRU.UN.TO `confirm_rsi_meanrevert` sized **1,500 shares** and filled, the first `confirm_*`
      fill in the journals. (`da6c798e…` predates the fix and never traded the three names.)
    * The alert's "walk-forward evidence" line shows the *base* strategy's registry params
      (e.g. `rsi_meanrevert window=7, oversold=25`) under a `confirm_rsi_meanrevert` entry — the
      same misreporting bug as above.
    * `CONFIRM_STRATEGIES` zero-arg construction passes `symbol=None`, so there's no asset-aware
      pattern filter on this path. Fine for equities; needed before any crypto use.

- 🟢 **Telegram bot token leak — RESOLVED 2026-09-17.** New bot and token are in `.env`, and the
  user confirmed the old bot is revoked, so the leaked token is dead. Session scratchpad logs are
  redacted. Leftovers (hygiene only, the token is invalid): `logs/trading.log` and
  `reports/logs/paper_qt_*.log` still contain the revoked token (user's files, gitignored).
- 🟢 **Telegram delivery — FIXED 2026-09-17.** Earlier in the session `getChat` returned
  400 "chat not found" and every alert was silently rejected; after the user contacted the new
  bot it returns **ok, private chat**, with `getMe` ok (`@Fintelligence_Notifs_bot`). No alert has
  actually been delivered yet (the fix landed after both sessions were stopped) — the next paper
  session's first fill is the real confirmation. Recheck recipe, read-only and sends nothing:
  `getMe` + `getChat?chat_id=$TELEGRAM_CHAT_ID`; if `getChat` fails, `getUpdates` lists the chats
  that have contacted the bot. Remember `_telegram` never checks the HTTP status (user's choice),
  so this out-of-band check is the only way to know. httpx logs every
  request URL at INFO, and Telegram's URL contains the bot token. It's in `logs/trading.log`,
  `reports/logs/paper_qt_2026-09-09*.log`, `reports/logs/paper_qt_2026-09-10.log` and the session
  scratchpad logs (all gitignored, never committed), and it also appeared in a Claude session
  transcript. Code fix landed (uncommitted): `logging_setup.configure_logging` pins
  `httpx` / `httpcore` to WARNING. **Still needed from the user:** revoke/regenerate the token via
  BotFather and update `.env`. Then purge or redact the old log files. Any QT process started
  before the fix keeps leaking until it's restarted.
- **Runtime-exercise the IB news → graph pipeline** (unit-tested only). With TWS on 7496:
  `IBBroker(port=7496).list_news_providers()`, then
  `IB_PAPER_PORT=7496 .venv/Scripts/python.exe scripts/paper_ib.py --transport socket
  --news-providers BRFG,FLY --news-cadence-s 30`. Check the `news drained N → M edges` lines and
  `mentioned_by` rows in `state/intel_graph.jsonl`. News records carry `meta.symbol=None`, and
  news adds one `reqMktData` line per symbol against the ~100-line cap.
- **First run of `scripts/screen_futures.py`** (never run; needs TWS on 7496, read-only):
  `IB_PAPER_PORT=7496 .venv/Scripts/python.exe scripts/screen_futures.py --years 2`.
  Expect little value while futures market data is missing (see Parked).
- **Asset-class calibration — remaining after the 2026-09-15 sweep.** The fold landed for 4 of 6
  cells (WFE ≥ 1.0). Still open:
    * Crypto `bollinger n_std` (WFE 0.35) and `zscore_ou entry_z` (WFE 6.20, degenerate) were left
      on the heuristic. The basket was n=3; widen it (needs the deep-history fetch below) and re-run
      `scripts/calibration_sweep.py`.
    * RSI `oversold=35` won at the top edge of `{20,25,30,35}` in both classes. Extend the grid to
      `{40,45}`.
    * The FX slice is blocked on the FX classification bug and on FX being dropped.
    * Confirmation filter: consider dropping `bullish_engulfing` on crypto (the gap criterion is a
      rounding artefact on continuous bars). Needs backtest evidence.
### Kraken data → graph → walk-forward → calibration (one chain, in order)

Four steps that only pay off in sequence: the fetch feeds the graph and the walk-forward, and the
walk-forward feeds the calibration. Doing 2–4 before step 1's data lands produces confident-looking
numbers from a 12-day-stale cache. Both fetch scripts hit Kraken's ~1 req/s public tier, so they
**must not run concurrently** — `scripts/kraken_interleave.py` (committed `b1cdabd`) serializes them.

- **1. Fetch — IN PROGRESS 2026-09-23.**
    * *Deep history* (daily bars, `fetch_crypto_history.py`): `_daily.parquet` existed only for
      BTC, ETH, PAXG. XMR started 2026-09-23 (~2.5M trades in ~45 min; 15000-page cap ≈ 4.4h).
      Remaining after it: ZEC, LINK, then XRP, XLM, SOL, ADA, POL, UNI, AAVE — **hours per pair**,
      so ~days of wall-clock. Only runs while a session is alive; an unattended schedule needs the
      user's consent first.
    * *Tick deepening* (`deepen_kraken_trades.py`): one pass ran 2026-09-23 (12 pairs, 14.4 min,
      `--max-pages 60`). That cap does NOT close the gap — the cache had been idle since 09-10, and
      60k trades buys ~14h of BTC. After the pass: POL current, AAVE 2d behind, LINK/XMR ~5d,
      ADA/XLM/UNI 7–8d, BTC/ETH/ZEC/SOL/XRP/PAXG 10–12.6d. So `trades_24h=0` and
      `buy_vol_share=n/a` for 11 of 12 pairs in `reports/kraken_trade_intel.csv`. Each interleaved
      pass moves every pair forward by ≤ `--tick-pages`×1000 trades; closing the gap needs several.
      PAXG is frozen by design (left the traded sleeve 2026-09-23).
    * **Done when:** every sleeve pair has a `_daily.parquet`, and the tick caches are < ~1 day
      behind so a 24h microstructure window is actually populated.

- **2. Wire the fetch output into the intel graph journal — NOT WIRED, design only.** Today neither
  script imports `intel.graph`: deepening writes per-pair parquets + `reports/kraken_trade_intel.csv`,
  the history fetch writes daily parquets, and **nothing reads that CSV** (a produced-but-unconsumed
  artefact). Proposal, unchanged in shape: new predicates `flow_imbalanced`
  (weight = `buy_vol_share − 0.5`) and `vwap_gap`, emitted by the batch job (never the paper loop),
  one edge per pair per run — **no per-tick edges**, or a single pass would dwarf the ~10k-edge
  journal. Prototype behind `--emit-edges` on `deepen_kraken_trades.py`.
    * *Implementation notes gathered 2026-09-23:* `Predicate` in `intel/graph.py` is a `Literal`,
      so both names must be added there **and** to `DEFAULT_POLICIES` — a predicate with no policy
      is kept unchanged by `wash_edges` forever (that is why `traded` / `ranked_by` never prune).
      Give them fast decay (`half_life_h≈24`, `ttl_h=72`) so stale flow can't linger. Subject/object
      should be the existing `symbol` / `venue` node types, so the edges join the fill (`traded`)
      records already in the journal.
    * *Gate before emitting anything:* step 1 must be current. Emitting now would write `n/a`-derived
      or 12-day-old flow as if it were today's.

- **3. Walk-forward on the deepened data.** `scripts/walk_forward_crypto.py` reads the deep parquets
  (2y train / 6mo test, per-fold re-opt; robust = WFE ≥ 0.5 AND positive OOS AND ≥ 10 OOS trades)
  and writes `reports/walk_forward_crypto.csv`. The 09-08 run had 11 of 13 pairs shallow, so its
  tiering is not evidence. Re-run per pair as each `_daily.parquet` lands; the script deliberately
  does **not** flip the `tier` field in `CRYPTO_SLEEVE` — a human reads the report and edits
  `universe.py`, so the sleeve isn't silently bound to the last Kraken pull.
    * Open question worth answering with the fuller data: does the order-flow feature from step 2
      add anything to a WF fold, or is it decoration? Test it as a feature, not as a live gate.

- **4. Calibration / tuning of the crypto cells.** `scripts/calibration_sweep.py` sweeps Bollinger
  `n_std`, ZScoreOU `entry_z` and RSI `oversold` on a class basket with the same WF harness, scored
  on `sortino_over_dd`, writing `reports/calibration_sweep.{csv,md}`. Its crypto basket was limited
  to whichever pairs had a deep cache, so the crypto cells rest on BTC/ETH/PAXG. Re-run once step 1
  widens it. Carry the known follow-up: **RSI `oversold=35` won at the top edge of {20,25,30,35}
  in both classes — extend the grid to {40,45}**, or the "winner" is just the boundary. The script
  does not edit `analysis/calibration.py`; a human folds winners in.
    * Standing rules that apply here: `sortino_over_dd` is primary (Sharpe is reference only);
      data-first — one WF pass validates the protocol, promotion also needs the paper A/B.
- **IB OAuth 1.0a for CP Gateway** (~4–6 hr). **Blocked on user setup**: consumer key, a
  **rotated** token plus its secret (see decision #1; the old value also sits in a session
  transcript under `.claude/projects/…/bac3334b-*.jsonl`), a local RSA-2048 signing key whose
  public half is uploaded to IBKR, and the DH constants. Build: `OAuth1Auth` in `brokers/ib_web.py`,
  `--auth oauth1` on `paper_ib.py`, empty-default secret fields, respx tests, `.env.example`.
- **Symbol atlas** (`analysis/symbol_atlas.py`, ~4–6 hr) — only if cross-venue trades get proposed.
  Gaps 1 (pre-flight validation) and 3 (IB socket routing) are closed.

## Data-blocked — revisit on date

- **Risk-guard validation analysis — due now (≥1 week post-09-08).** Per-gate firing rates and
  threshold sensitivity (especially a `force_exit_atr_mult` sweep over {2.0…4.0}) from
  `paper_orders.jsonl`, `paper_fills.jsonl` and `paper_equity.csv`, written to
  `reports/risk_guard_analysis_<date>.md`. **Caveats found 2026-09-16:**
    * **Size-cap trims aren't journaled.** A trim is an accepted order with fewer shares, so it
      never appears in `rejected_reasons`. The trim rate can't be measured from the journal as-is.
      Observed only via alert vs fill: VDY.TO 1294→165 (`4a6ad96a`), 1293→578 (`b14e4de0`); ENB.TO
      1142→684. Log requested vs filled shares first.
    * The journals mix orphaned books and default-params sessions (see the bugs above).
    * `check_forced_exits` has no caller, so there's no forced-exit data to analyse.
- **Strategy-level risk stops** (`ts_momentum trail_atr_mult=4.0`; mean-reversion
  `time_stop_bars=20`; trend `stop_atr_mult=3.0`). Each must improve `sortino_over_dd` in walk-forward
  before it lands.
- **Cross-path tiers 4 + 5** (realized P&L → thesis calibration; prediction evaluation). Need weeks
  of fills matched to theses, and 21-day forward windows. Revisit ~2026-Oct / Nov.
- **OSINT × commodity-proxy correlation study** (spec frozen 2026-09-05). Response: forward
  log-returns at h∈{1,5,21}. Features per domain: raw scalar, 90d z-score, persistence count.
  Controls: 21d momentum and 21d realized vol. OLS with Newey-West errors; BH-FDR α=0.10 over
  294 tests. Proxies: USO UNG GLD SLV PPLT CPER WEAT CORN SOYB TLT IEF HYG UUP VXX. Needs ≥3 months
  of intel journal (~2026-Dec); full scope ~2027-Mar. Until then only the proxy data pipeline and
  the regression harness may be built — don't run it on the shallow corpus.
- **The `enrich_with_agents` / `intel/agents.py` LLM debate layer** — built and tested, never
  called. Held until there's an `ANTHROPIC_API_KEY`, weeks of graph depth, and a specific hypothesis
  worth the round-trip.
- **Thesis threshold drift.** The 2026-09-16 `interpret.py` cutoffs (`STRATEGIC_RISK_STRESSED=73`,
  `CONFLICT_EVENTS_ELEVATED=6`) are fitted to one 18-day regime. Re-measure base rates as the
  corpus grows; consider a trailing-percentile gate. Four gates have never fired on observed data
  (listed in the `interpret.py` module header).

## Held for market data (IB subscriptions / L2 depth)

The items below wait on market-data access that isn't in place: real IB market-data
subscriptions (US futures bundle, plus the global equity / ICE Europe / SGX / OSE feeds as needed)
and, for microstructure, Level-2 depth-of-book. Without subscriptions, error 354 means an IB-fed
book launches and never trades. Revisit when the data exists.

- **Microstructure controls beyond top-of-book — HELD on L2 market data (decided 2026-09-16).**
  Same treatment as exchange hopping: parked, not abandoned.
    * **Active today (top-of-book and exchange rules only, per the 2026-09-13 decision):**
      `execution/scheduler.py::MicrostructureConfig` — spread ceiling (50 bps equity / 30 bps
      crypto, entries only; exits are never blocked), board lots, open/close auction buffers,
      intent TTL, touch fills.
    * **Built but not wired into any trading path:**
        * `microstructure/` — `kraken_l2`, `coinbase_l2` and `bitstamp_l2` book streams,
          `orderbook`, `simulator`, `arbitrage`, `cross_exchange`, `avellaneda_stoikov`,
          `live_market_maker`, `interlisted`.
        * The `kraken-l2` CLI (read-only microprice / imbalance / OFI).
        * `IBBroker.request_l2_book` (socket only; needs a deep-book subscription per exchange).
        * `scripts/liquidity_heatmap.py` (one-shot, 09-05 run).
        * `scripts/deepen_kraken_trades.py` tick caches.
    * **Held until L2 data is available in the pipeline:**
        * The rolling microstructure accumulator (spec in git history, pre-2026-09-16 revision)
          → deeper heat maps → `LiquidityGate` (a size multiplier by hour×weekday liquidity,
          trim-only first; fail-open everywhere).
        * Depth-aware entry/exit controls: book-based impact/slippage estimates, microprice / OFI
          gating, and depth-scaled sizing in place of the flat spread ceiling.
    * **Before activating:**
        1. L2 source per venue. IB needs a deep-book subscription per exchange. Crypto venues
           publish L2 free, but it isn't wired into the paper loop.
        2. Evidence that cold-liquidity periods actually cost more: realized slippage cold vs hot,
           e.g. ≥2×. Otherwise the gate cuts size on a proxy that doesn't cost.
        3. ≥90 days of accumulated density (data-first rule).
        4. Ask before registering any scheduled collector.
    * Not part of this hold: the order-flow **intel-graph** edges from Kraken public trade ticks
      (build queue). They are research features from trade prints, not depth-based controls.

- **IB paper route: derivatives + overseas exchange hopping — UN-HELD 2026-09-21 (user decision),
  🟡 CODE-COMPLETE, UNIT-TESTED, NOT RUNTIME-EXERCISED** (TWS had no API port open).
  Decisions: **per-currency books** (no IB FX; single-currency guard kept), **live market data shared
  to the paper login**. `desk_policy.assert_ib_no_questrade_equities` lets .L/.AX/.T/.HK through
  and still refuses US/TSX/TSX-V (Questrade's). `scripts/paper_global.py` gained `--equities`
  (overseas only), `--client-id` (one per book on a shared TWS), `--min-ticket` (book currency),
  `--resume-session`, `--flatten-on-exit` (default on) + `STOP_<id>`. Futures rows in another currency
  are skipped per book. Non-CAD/USD books refuse to launch without explicit `--paper-equity` and
  `--min-ticket` (`require_explicit_book_sizing`). Caveat: a book stopped while its venue is closed
  can't flatten (SessionRouter queues the sell) — `monitor.flatten.incomplete` fires; stop it while
  open, or use `--no-flatten-on-exit` + `--resume-session`. **Next:** enable the TWS API on 7497
  (paper login), probe one quote per venue (live vs error 354), pick a basket per venue from the
  probe (lot sizes, LSE pence), then launch, e.g.:
  `paper_global.py --crypto "" --futures config/futures_universe.json --numeraire USD --client-id 51`
  (CME micros + SGX iron ore/rubber, which trade in Asia hours) and a JPY book (OSE futures + .T
  names, `--paper-equity 15000000 --min-ticket 15000 --client-id 52`).
  * **Probe 2026-09-21 (paper login DU***99 on 7497; 7496 not listening):** live quotes error 354 on
    every venue incl. SPY, twice — before and right after the user enabled real-time sharing (IB
    applies it at the next paper login, up to 24h). Daily bars work everywhere except Tokyo (error
    162, no TSEJ permission). All futures fronts resolve. Re-run: scratchpad `ib_probe.py 7497`.
  * **Pence / lots / HK — FIXED 2026-09-21, unit-tested + checked on real IB bars:**
    `IBBroker.stock_details()` (cached contract details) divides stock quotes and bars by
    `priceMagnifier` (BP. 542.3p → £5.423; ISF 100, VUSA 1 — varies per listing, so no venue rule)
    and multiplies live limit prices back. Fails closed (BrokerError) when details are missing.
    `paper_global.py` takes board lots from `sizeIncrement` (1306.T 10, 7203.T 100, 2800.HK 500,
    700.HK 100); trading.yaml `board_lots` still wins.
  * **Still open:** LSE ETFs list on `LSEETF`, but `.L` routes to `LSE`, so ISF.L / VUSA.L don't
    resolve (BP..L does). Tokyo needs a TSEJ market-data permission.

- **Off-hours coverage — the four options (laid out 2026-09-22, UNDECIDED).** No IB book can trade
  until live quotes exist; these are the ways forward, cheapest first. Recommendation: do 1, then
  2 while deciding on 3, and buy the US futures bundle before any Asia equity data — nothing on
  this route has been runtime-exercised yet, so paying for Asia equity data now buys untested
  plumbing.
    1. **Re-test the sharing (free, ~5 min).** IB applies shared market data at the next *paper
       login*; neither 2026-09-21 probe followed a fresh login. Start TWS, log into paper, re-run
       the scratchpad `ib_probe.py 7497`. Could clear everything except Tokyo.
    2. **Run the books on daily bars instead of live quotes (free, small change).** Bars already
       work for US / LSE / ASX / HK / all futures, and the strategies are daily-bar strategies —
       only the *fill* needs a quote. Synthesize the fill from the last bar, marked stale, to
       exercise routing, gates, lots, pence, rolls and session hopping end to end. Gives plumbing
       evidence only: no honest fill price, no spread, so P&L means nothing.
    3. **Buy the subscriptions.** The only path to real quotes. Highest value first: the **US
       futures bundle** (CME/NYMEX/CBOT) — futures run ~23h, rolls are wired, the USD book is
       ready. Tokyo needs one before even its bars work. ASX / LSE / HKEX are separate additions.
       Prices are per-exchange monthly fees in Client Portal → Market Data Subscriptions (some
       waived above a commission threshold); don't quote them from memory.
    4. **Reconsider what actually covers the gap.** The goal is coverage outside Canadian hours.
       Futures (~23h, USD) plus the existing 24/7 Kraken crypto book cover most of it for one
       subscription. Asian/European *equities* add genuinely different exposure but cost the most
       and carry the most unfinished work (Tokyo permissions, LSEETF routing, per-name lots).

- **Options (the instrument) — NOT WIRED, scoped 2026-09-22.** `brokers/ib.py`'s header advertises
  "equity and index options with the full Greeks surface"; what exists is contract *construction*
  only. Concretely:
    * **Exists:** `IBContract(sec_type="option", strike=…, right="C"/"P", expiry=…, multiplier=…)`
      builds an `ib_insync.Option` via `_to_ib_contract`, reachable through `quote_contract()` and
      `place_algo_order()` — both IBContract-level, outside the symbol-string `Broker` protocol.
      `scripts/probe_ib_data.py` names chain resolution (`/iserver/secdef/strikes` + `/info`) on
      the Web API, but no adapter method implements a chain walk.
    * **Missing before an option could ever be a position:** (a) a symbol grammar — `venues.py`
      reads `/ROOT` as a future and `BASE/QUOTE` as crypto, everything else as an equity, so an
      option can't be named in `--symbols`; (b) no chain/expiry/strike selection anywhere;
      (c) `Quote` carries no IV or Greeks fields, so nothing can price or hedge one; (d) the
      contract multiplier reaches the book only through `CurrencyNormalizingBroker.multiplier_for`
      (futures path) — `PaperBroker` otherwise fills shares × price, so a 100-multiplier contract
      would book at 1/100 of its real notional; (e) sizing/risk is ATR-on-the-underlying
      fixed-fractional — premium-at-risk, delta and assignment have no representation, and the
      heat/kill-switch math would read premium as notional.
    * **If it's ever wanted, the honest order is:** decide the use (covered calls / protective puts
      on existing equity exposure vs directional vs vol) → symbol grammar + chain resolution →
      multiplier through the paper fill path → Greeks on the quote model → an options-aware sizer
      → only then a paper book. Needs an options market-data subscription too (OPRA for US).
      Treat as a project, not a flag.
- **Exchange hopping, levels 2–4 — HELD (decided 2026-09-16).** Not abandoned: IB stays in the
  stack, and these levels resume once market-data access exists. *(Superseded 2026-09-21 by the
  entry above for per-currency books; L3 multi-currency and L4 remain held.)*
    * **Already built (2026-09-13, `2dd2043`):** `venues.py` (suffix → IB exchange / currency /
      hours / board lot for US, TSX, TSX-V, LSE, ASX, Tokyo, Hong Kong); the closed-venue skip
      (L1, active); venue-routed IB socket quotes and candles; the CAD numeraire via IB spot FX
      (`brokers/fx.py`); `SessionRouter` queueing of closed-venue intents with release after the
      open buffer through all gates; `scripts/paper_global.py` as one book over IB + Kraken;
      spread ceiling, board lots, auction buffers, touch fills, Dimson ±1 lead-lag correlation.
    * **Currently gated off** by the desk-policy WIP (`desk_policy.py` refuses equities and
      mixed-currency baskets on the IB paths). Re-enabling L2–4 means revisiting that guard then.
      Don't strip it now.
    * **Before trading any new venue:**
        1. Market-data subscriptions for that venue.
        2. Per-symbol walk-forward before any foreign name enters `WALK_FORWARD_VALIDATED`.
        3. Note that the US-centric OSINT overlay gives Asia and Europe names no region-specific
           intel.
    * **L3 (multi-currency accounting) caution:** FX moves become a new risk vector. A book that is
      flat in native currency can trip the drawdown kill-switch through FX alone. Numeraire-based
      math is needed across KillSwitch / heat / the allocator covariance. Test an FX-shock scenario
      before going live.
    * **L4 (24h scheduler + non-overlapping correlation):** a hypothesis, not a spec. Design it
      only after L2 and L3 have live evidence.
    * **Known limits of what's built:**
        * No exchange-holiday calendar (the stale-quote guard covers it).
        * LSE pence quoting unverified.
        * Native-currency returns in the covariance.
        * In-memory intent queue (lost on restart).
        * FX needs `--transport socket`.
- **Futures strategy research — removed 2026-09-18.** See Parked. The market-data subscription
  is also a prerequisite for reviving it.

## Exit variants — parked for comparison

Candidate exit-rule changes kept side by side so they can be compared on the same harness before any
goes live. None is enabled. Compare each against the no-change baseline on `sortino_over_dd`, **and**
on Sortino, return and time in market, because a variant that just sits out can "win" the ratio
through a smaller drawdown alone.

- **Variant #1 — profit-lock ratchet (parked 2026-09-18; folded into paper 2026-09-18 15:27 ET).**
    * **Now running on QT paper** session `ec72c96f…` with `--profit-lock` and the new
      `--profit-lock-exempt` (default `ts_momentum`). At launch the lock covers only SRU.UN.TO of
      the held names; EQB.TO, QQQ and VDY.TO are exempt `ts_momentum`. First paper exposure: judge it
      over weeks of sessions against the earlier no-lock sessions, not on one day.
    * Code in place, **off by default**: `signals/profit_lock.py`; hooked into
      `SignalSet.to_positions(profit_lock=..., round_trip_cost_frac=...)`,
      `BacktestEngine.run(profit_lock=...)` and `LiveMonitor(profit_lock=...)`. The QT flag is
      `cli.py signal --profit-lock` (QT only; not wired into `paper_kraken.py`).
    * Rule: arms at +1 ATR; exits when price falls back from the peak by more than a giveback that
      shrinks on `log10(1 + 9u)` from 3 ATR (at +1 ATR) to 1 ATR (from +4 ATR).
    * Cost cross-check: floors at net breakeven (entry + round-trip cost) and arms only once the gain
      covers 2× the round-trip cost.
    * Re-entry lockout: after a lock exit, no re-buy until the entry signal has switched off once.
      Without it, level entries (`ts_momentum`) churn a round trip per poll live.
    * Evidence: `reports/profit_lock_backtest_2026-09-18.md`. In-sample, one a-priori configuration,
      2–43 trades per name. Equal-weight 14-name QT book at realistic costs, sortino/DD:
        - no lock: 45; Sortino 2.21, DD −4.9%, +39.4%, 29% in market;
        - lock on all 14: 74; Sortino 1.89, DD −2.6%, +21.3%, 14% in market. It cuts trend
          winners: QQQ +97% → +4%;
        - lock except `ts_momentum`: 65; Sortino 2.27, DD −3.5%, +37.8%, 27% in market. The benefit
          holds at 2× costs.
      Gains concentrate in ZEB.TO, XIU.TO and VALE; CGL.TO and SRU.UN.TO are worse.
    * Unit-tested (`tests/test_profit_lock.py`, 19 tests); never runtime-exercised.
    * If revived: add a per-strategy exemption (`ts_momentum` first), walk-forward it rather than
      trusting the in-sample run, then a paper session with the flag on.
    * Kept regardless of the variant: exit alerts now carry the real reason (stop / profit_lock /
      flatten) instead of always "strategy exit signal".

- **Variant #2 — candlestick exit (built + backtested 2026-09-18; NOT recommended, off).**
    * Code in place, **off by default**: `signals/candle_exit.py`; hooked into
      `SignalSet.to_positions(candle_exit=...)`, `BacktestEngine.run(candle_exit=...)` and
      `LiveMonitor(candle_exit=..., candle_exit_exempt=...)`; QT flags `--candle-exit` /
      `--candle-exit-exempt`.
    * Rule: sell a long that is up ≥ 1 ATR and ≥ 2× round-trip cost on the bar after a bearish
      reversal completes (the 8 mirrors of the entry-confirm set). Shares the re-entry lockout;
      live reads the last completed daily bar.
    * Evidence: `reports/exit_variants_backtest_2026-09-18.md` (same harness as #1, in-sample, one
      config). Portfolio at realistic costs, sortino/DD / Sortino / return:
        - no change: 45.2 / 2.21 / +39.4%;
        - V2 on all 14: 38.0 / 1.46 / +17.3% (time in market 29% → 12%);
        - V2 except `ts_momentum`: 41.2 / 1.98 / +33.6%, DD barely moves (−4.9% → −4.8%);
        - V1 + V2: 64.8 / 2.19 / +35.9%, no better than V1 alone (65.0 / 2.27 / +37.8%).
      It gets worse as costs rise (+35 trades), and it never fires on XIC.TO, VALE, CRT.UN.TO,
      ENB.TO or XIU.TO. Consistent with `strategies/overlay.py`'s finding that candles are a
      precision lever only at mean-reversion *entries*.
    * Unit-tested (`tests/test_candle_exit.py`, 10 tests); never runtime-exercised.

- **Variant #3 — overbought (mean-reversion) exit (built + backtested 2026-09-18, off).**
    * `signals/overbought_exit.py`. Sells a long that is up ≥ 1 ATR and ≥ 2× round-trip cost on the
      bar after a close above the upper Bollinger band (20, 2 sd) or RSI(14) ≥ 70. A run with no
      down days is scored RSI 100. It shares `WinnerGate` (arming) with #2, plus the re-entry
      lockout and live completed-bar reading. QT flags `--overbought-exit` /
      `--overbought-exit-exempt`. Unit-tested (`tests/test_overbought_exit.py`, 8 tests);
      never runtime-exercised.
- **STANDING configuration (user decision 2026-09-18, revised the same day): V1 + V2 + V3 with
  `ts_momentum` exempt from all three** on QT:
  `--profit-lock --profit-lock-exempt ts_momentum --candle-exit --candle-exit-exempt ts_momentum
  --overbought-exit --overbought-exit-exempt ts_momentum`. The momentum names (EQB.TO, QQQ,
  VDY.TO) keep only their strategy exit; the other 11 names take profits on the lock, a bearish
  reversal candle, or an overbought reading. Not launched yet: QT was closed when it was set.
  Launch it at the next open (runbook 4b).
    * Backtest (`reports/exit_variants_v123_backtest_2026-09-18.md`, in-sample, one a-priori config,
      2–50 trades per name). Portfolio, sortino/DD / Sortino / max DD / return / time in market:
        - realistic costs: no change 45.2 / 2.21 / −4.9% / +39.4% / 29% → **standing 68.9 / 2.15 /
          −3.1% / +34.1% / 25%**;
        - 2× costs: no change 38.0 / 1.97 / −5.2% / +34.6% → **standing 52.8 / 1.82 / −3.5% / +28.3%**.
      So it trades ~5 pp of return and a little Sortino for a ~1.8 pp smaller drawdown, and that
      holds when costs double. Gains concentrate in XIU.TO, VALE, XIC.TO and RSI.TO; CGL.TO,
      SRU.UN.TO and ZEB.TO give up return.
    * Superseded the same day: the mix that exempted `ts_momentum` from V1 only (V2/V3 still cut the
      momentum names: 64.3 / 1.59 / +16.7%, worse than no change at 2× costs).
    * Judge it on paper over weeks against the no-change sessions, on Sortino, return and time in
      market as well as sortino/DD. Not walk-forward validated.

- **Variant #4 — buy on oversold (planned 2026-09-18, not built).** User idea to win back the return
  V1–V3 give up. Role (user decision): **trend pullbacks only**. Trend-family names buy oversold
  dips (lower Bollinger band or RSI <= 30 on the last completed bar) while their trend signal is up.
  Designed in "Plan — robust entry/exit signal system", Phase 2.
- **Scope of all exit/entry variants: the QT book, all 14 names, DBC and CGL.TO included (user
  decision 2026-09-18).** Crypto stays on its own strategy exits; no Kraken wiring.

## Parked

- **Futures strategy research — removed 2026-09-18.** The QC futures research harness is deleted
  (`integrations/lean_futures.py`, `scripts/qc_futures_empirical.py`, the shelved WF stash and the
  `qc_futures_*` / `qc_trend_zscore_*` / `qc_xsec_momentum_*` reports). Come back to it with a
  refreshed approach and a more thorough baseline. **LSTM-embedded volatility-weighted momentum**
  is deferred to a later horizon. The IB futures paper path (`futures.py`, `data/futures_*`,
  `paper_ib.py` / `paper_global.py`) is unaffected. The QC projects `frm-futures-empirical`,
  `frm-trend-zscore-wf` and `frm-xsec-momentum-wf` are kept on QuantConnect.
- **Interlisted TSX⇄NYSE arb** (`microstructure/interlisted.py`): 0/25 pairs clear at retail FX.
  If revisited, scan during market hours with a stale-quote filter and streaming quotes.
- **Live crypto routing** through `AssetRouter`, only once the go-live decision is made.
- **Tick-aware fill prices** in `PaperBroker`, alongside tick-aware router sizing.
- **Correlation matrix snapshots** to `state/{crypto,equity}_corr.jsonl` on a schedule (needs consent).
- **Carry-inversion thesis** on a real futures-curve feed instead of the stress+flow proxy.

## Open audit gaps (from 2026-09-04, re-verified 2026-09-16)

🔴
- ✅ FIXED 2026-09-18: `_atr_or_default` (NaN / inf / <= 0 / non-numeric -> 2% of price), tested. Was: `monitor/live_loop.py` (~L357): `float(last.get("atr", …)) or price*0.02` passes NaN through,
  because NaN is truthy. Sizing then runs off NaN.
- ✅ FIXED 2026-09-18: capped at `MAX_ORDER_CONFIRMATIONS = 5`, then `OrderRejected`, tested. Was: `brokers/ib_web.py` (~L503): the `place_order` auto-confirm `while` loop has **no cap**. In live
  mode it accepts warnings a human should see, and it could loop forever.
- ✅ FIXED 2026-09-18: the README step 7 now points to the pre-flight checklist instead of a copy-paste live command. Was: `README.md:141` showed `EXECUTION_MODE=live …`.
- 🔴 **`QUESTRADE_ENV` selects nothing (found 2026-09-18, needs your decision).** Deeper than the
  audited "`os.environ` override set after settings load" (`cli.py` autonomous `run`, which is
  indeed a no-op): `QuestradeBroker` never reads `questrade_env`. It always logs in at
  `login.questrade.com` (`LOGIN_HOST` hardcoded) and trades whatever account the **refresh token**
  belongs to. `questrade_env` is only a label (`settings.is_live_capable`, the `trading live` check).
  Paper sessions are unaffected (PaperBroker never sends orders to Questrade). **Risk:** the
  autonomous daemon with its default `AUTONOMOUS_ACCOUNT=practice` and a *live* token in `.env` would
  place real orders on the live account. Options: (a) map practice to Questrade's practice login
  host (verify it still exists) and pass it from `questrade_env`; (b) verify the account type from
  `/v1/accounts` at boot and refuse on mismatch; (c) refuse `autonomous start` unless
  `AUTONOMOUS_ACCOUNT == QUESTRADE_ENV == "live"` is set by the human. Not changed: the live and
  autonomous paths are yours to decide.

🟡
- ✅ FIXED 2026-09-18: SHA-256 fold; a cross-process test pins stability. Was: `brokers/kraken.py::_txid_to_int` uses `hash()`, which is randomized per process, so order ids
  change across restarts.
- ⏸ MEASURED 2026-09-18, deferred: 300 lines / 90 KB, 5.4 ms per snapshot, autonomous-only. Rewriting a risk gate for that is risk without benefit; revisit past ~100k lines. Was: `execution/daily_budget.py::snapshot` re-parses the whole `orders.jsonl` on every gate check.
- ✅ FIXED 2026-09-18: temp file + `os.replace`, first tests for the cache (`tests/test_candle_cache.py`). Was: `data/cache.py::put` writes without an atomic rename. `cache.py` paths carry no schema version,
  and `data/market.py` has no incremental tail fetch.
- ✅ VERIFIED NOT A BUG 2026-09-18: `existing_risk` is dollars (`portfolio_risk` aggregates per-position dollar risk), so `existing_risk / equity` is already a fraction, matching `HedgePolicy.heat_ref = 0.05`; `heat_boost` defaults to 0 anyway. Was: `monitor/live_loop.py` (~L553): `heat = existing_risk / equity` is dollars vs fraction when the hedge is on.
- ~~`brokers/models.py`: the `Fill.venue` Literal predates multi-venue.~~ ✅ FIXED 2026-09-17
  (`0a13789`) — open `str` + `CANONICAL_VENUES` drift test; PaperBroker now stamps the resolved
  feed venue instead of hardcoding "paper".
- ~~`execution/asset_router.py`: `AssetClass` has drifted from `OverlayClass`.~~ ✅ FIXED
  2026-09-17 (`0a13789`) — now an alias of `OverlayClass` with mappings for fixed_income /
  precious_metals / fx and a drift test. Still true and deliberate: the brokerage ids are LEAN
  deployment targets, not this repo's runtime adapters.
- `intel/apply.py::apply_overlay` has no runtime caller.
- 🟡 **`.gitignore` gaps (filed 2026-09-17).** Two, both found while mapping the repo vs the
  working tree:
    * ✅ FIXED 2026-09-18: `state/*` + `!state/.gitkeep`. Was: **`state/` uses a deny-list, not `state/*`.** Each journal is named individually, so any NEW
      state file is tracked by default — `state/sizing_decisions.jsonl` and
      `state/intel_graph.jsonl.bak` both show up as untracked right now, and every future journal
      will too. Fix: `state/*` plus `!state/.gitkeep`.
    * **`reports/*.md` is ignored while PNG/CSV/JSON are tracked** — the polarity is backwards for
      a research repo. 78 binary PNGs are versioned (reports/ is ~11 MB, larger than `src/` plus
      `tests/`) while the markdown write-ups that carry the interpretation are local-only, so a
      committed chart has no committed explanation.
- ✅ FIXED 2026-09-18: `venue: str` declared (all six brokers already set it). Was: `brokers/base.py`: the `Broker` Protocol doesn't declare `venue`.
- ✅ FIXED 2026-09-18: `trading live` now exits (code 2) before building the broker, and the misleading "routing through practice account" message is gone (see the 🔴 `QUESTRADE_ENV` item). CLI test added. Was: `cli.py` `trading live` warns but doesn't abort when `QUESTRADE_ENV != "live"`.
- ✅ FIXED 2026-09-18: `CPGatewayAuth` refuses unverified TLS to a non-loopback host (at construction and in `new_client`), tested. Was: `brokers/ib_web.py`: `verify_ssl=False` with no guard that the host is localhost.
- Missing tests — PARTLY DONE 2026-09-18: `data/cache.py` (`tests/test_candle_cache.py`) and
  `execution/journal.py` + `DailyBudget` counting (`tests/test_order_journal.py`) added;
  `data/market.py` was already covered (`tests/test_market_data.py`). Still untested: the futures
  pipeline, `microstructure/*`, `intel/{apply,chart,worldmonitor}.py`, several scripts.

🟢
- ✅ FIXED 2026-09-18: the README lists all 44 registered strategies by family plus the exit
  layers; the execution row names real modules (no `execution/paper.py` / `live.py`); the skills,
  commands and agents lists match `.claude/`. `CLAUDE.md` gains `claude-trader` and the full
  command list. Its entry points were checked and are current. Was: "The README strategy table is
  stale … README and CLAUDE.md disagree on the agent set, and CLAUDE.md's entry-points list is stale."
- ✅ FIXED 2026-09-21: per-instance counter, continued by `resume()`; tested. Was:
  `PaperBroker._order_counter` is a class variable, shared across instances.
- ✅ FIXED 2026-09-21 (per-file random salt, legacy still read): `brokers/token_store.py` used a fixed PBKDF2 salt.
- `pyproject.toml`: dependencies have no upper caps; `ib_insync` is unmaintained (consider
  `ib-async`); PyJWT isn't declared.
- `brokers/questrade.py`: `LOGIN_HOST` is hardcoded.
- Older journal rows lack `session_id`, so joins must tolerate NULL.
- ✅ CHECKED 2026-09-18, not changed: `Router.submit` runs `_gate` (kill-switch first) *before* journaling. A rejection writes one `orders.jsonl` row (`accepted: false`) plus one `rejected.jsonl` row: two journals, by design, not a duplicate. The real effect is volume, since a halted session re-submits every poll. Was: "The router journals an intent before the kill-switch check, which duplicates rows when halted."
- ✅ FIXED 2026-09-18: still never raises, but logs `error_type` / `error`, tested. Was: `intel/graph.py::append_edges` swallows all errors, so a full disk drops writes silently.
- `starting_equity` and `_INTERPRET_BIAS_FLOOR` are hardcoded rather than configurable.

## TradeCard (§11) — open work

**⚠️ The approval axis does not currently work in paper mode** — the shim 500s on every paper-mode
prompt, so no card approval can complete and no card-gated order can fill. See the 🔴 entry at the
top of "Queue — correctness bugs". Everything below assumes that is fixed first; until it is, the
end-to-end claim in this section is aspirational, not verified.

**Architecture (decided 2026-09-08):** zero vendor infrastructure. Releases go through GitHub
(sigstore-signed), and the PWA and docs through GitHub Pages. Push goes via a user-chosen relay. The
shim is single-owner-per-box, with no accounts or telemetry. See memory
`tradecard-zero-vendor-infra-architecture.md`.

**Branch status — corrected 2026-09-17. There is no TradeCard PR to open.** The earlier note said
`feat/tradecard-approval` was pushed and awaiting a PR. In fact that branch is **fully contained in
`feat/multi-scoring-attention-map`** — 0 unique commits, 25 behind — so a PR into the working branch
would be empty, and a PR into `main` would be 133 commits / 261 files / +38,538 lines of the whole
long-lived line, not TradeCard-specific work (it carries the risk/router, bulk-catchup and
symbol-validation commits too). The user declined a catch-up PR of that size on 2026-09-17 and chose
to keep the feature branch as trunk. The branch tip is preserved as the pushed tag
`archive/tradecard-approval`. **The user chose 2026-09-17 to keep the branch and its worktree at
`C:/Users/PC/Downloads/FRM-Claude-tradecard` in place (clean, 0 changes) — do not propose deleting
either.** It is harmless, just redundant, and this note explains why it exists. If a reviewable TradeCard PR is ever wanted, it needs a fresh branch off `main` with only
the approval / shim / firmware / PWA paths cherry-picked — expect conflicts, those files have moved
a long way since `main`. `gh` 2.101.0 is now installed and authenticated, so tooling is no longer
the blocker.

**Follow-up PRs:**
- `feat/tradecard-multi-broker`: `Router(brokers: dict[str, Broker], default: str)`, dispatch by
  `intent.broker`, with the WYSIWYS canonical bound to the dispatched broker. The rebase touches
  `Router._gate`, so take care.
- `feat/tradecard-settings`: signed `SettingsChangeIntent` → `POST /v1/settings` → mutate
  `trading.yaml`, journaled with verdict `S`. On the card, add a SETTINGS menu and NVS-enforced
  Tier-A rules.

**Hardware:** no schematic, BOM or PCB. No secure element (the key sits in NVS; target ATECC608A or
the S3 DS peripheral). No power path or enclosure. No display or input sourcing decision. No RF
certification or antenna choice.

**Firmware:** the LCD now renders text — `main/font5x7.h` (5×7, ASCII 0x20–0x7E) plus a real
`lcd_puts_ex` painting into `s_fb`, inverse-video rows, a 14-column prompt layout with one
signed field per row, a fixed-deadline countdown, a panning thesis, a passbook empty state and
boot status screens (2026-09-23). **Code-complete, NOT compiled** — no ESP-IDF on the box; the
font and layouts were verified by porting the renderer to a host harness and rendering the real
canonical strings from session `24a7fd7f`. `idf.py build` is still the gate. Remaining: no TLS on card↔shim; no deep
sleep; no CENTER detail view (`GET /intel/{ref}` isn't called); trust-on-first-use pairing; no OTA;
no factory reset; no fault UI; no passbook search; no long-press handling. Firmware changes from
2026-09-13 aren't compiled yet (`idf.py build`).

**VS engine:** rules only (no optional LLM narrator); no confidence or attribution in the thesis;
no historical-comparison clause; no verdict feedback loop; broker→asset-class is 1:1;
`state/intel_writeups/` grows unbounded; no FR/EN; strategies don't emit `score` / `rank` /
`r_multiple`.

**API:** no SSE/WebSocket push, admin listing endpoint, rate limiting or CORS controls; mTLS per
card is still the production target; single-broker Router.

**UX:** no onboarding, queue preview, "why" screen, haptics or LED, PIN, timeout warning, passbook
filter, language setting or accessibility sizing.

**Connectivity:** Wi-Fi only (no BLE or cellular); no status indicator; no offline queue; SSID/PSK
fixed at build time; no mDNS; no captive-portal handling; no shim handoff; no card→shim health
telemetry; the shim is loopback-only with no scripted remote path.

**Sequencing:** firmware font → hardware SE once the firmware signs via I²C → multi-broker together
with strategy `MarketContext` adoption → UX and connectivity polish once the card is a physical object.

Published brief: <https://claude.ai/code/artifact/9567c2ac-b2bd-4797-adf5-2edbce2a4d90>
