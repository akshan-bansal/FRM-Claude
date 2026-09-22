# Next-session backlog

**Pruned 2026-09-16** to active items only. Everything removed was either shipped, run,
superseded, or closed by an explicit user decision; the full pre-prune text is in git history
(last committed version: `4d471f3`) and in the session scratchpad backup
`NEXT_SESSION.backup-2026-09-16.md`.

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
    --symbols "EQB.TO,QQQ,XIC.TO,ZEB.TO,CGL.TO,VALE,DBC,SRU.UN.TO,CRT.UN.TO,ENB.TO,XIU.TO,VDY.TO,SLF.TO,RSI.TO" \
    --strategy-map "EQB.TO=ts_momentum,QQQ=ts_momentum,XIC.TO=rsi_meanrevert,ZEB.TO=atr_channel,CGL.TO=atr_channel,VALE=bollinger,DBC=bollinger,SRU.UN.TO=rsi_meanrevert,CRT.UN.TO=rsi_meanrevert,ENB.TO=bollinger,XIU.TO=bollinger,VDY.TO=ts_momentum,SLF.TO=bollinger,RSI.TO=bollinger" \
    --interval 300 --paper --paper-equity 100000 --level --intel-overlay
.venv/Scripts/python.exe scripts/paper_kraken.py --interval 300 --paper-equity 100000

#    Optional warm-up (2026-09-17): poll faster for the first hour to re-establish positions after a
#    flatten, then fall back to --interval in the same process. Add to either command:
#      --warmup-interval 60 --warmup-minutes 60
#    QT's current map runs ENB.TO=confirm_bollinger, SRU.UN.TO=confirm_rsi_meanrevert, XIU.TO=composite.

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
#     BUG: partial sells don't book realized_pnl. See "Queue — correctness bugs" (2026-09-22).

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

- 🔴 **Paper broker doesn't book realized P&L on a partial sell.** Found 2026-09-22.
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
- **Deep-history fetch for the crypto sleeve.** `_daily.parquet` exists only for BTC, ETH and PAXG.
  Remaining priority: XMR, ZEC, LINK, then XRP, XLM, SOL, ADA, POL, UNI, AAVE
  (`scripts/fetch_crypto_history.py --pair <WIRE> --since 2020 --max-pages 15000`). This takes
  multiple hours per pair at ~1 req/s. An overnight scheduled run needs the user's consent first.
  It unblocks crypto tiering (the 09-08 WF ran 11 of 13 pairs shallow) and the crypto calibration
  cells.
- **Kraken tick-level deepening on a schedule.** `scripts/deepen_kraken_trades.py` works (13 pairs
  cached under `data/cache/kraken_trades/`). Proposed every 2h:
  `--lookback-hours 6 --max-pages 30 --sleep 1.1`. **Ask before creating any scheduled task**; if
  approved, commit the task definition to `scripts/`.
- **Order-flow edges in the intel graph (design).** New predicates `flow_imbalanced`
  (weight = `buy_vol_share − 0.5`) and `vwap_gap`, emitted by the same batch job (not the paper
  loop). Fast decay (`half_life_hours≈24`, `hard_ttl_days=3`). No per-tick edges. Prototype behind
  `--emit-edges` on `deepen_kraken_trades.py`.
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

**Firmware:** the LCD is only a UART mirror (no font/framebuffer); no TLS on card↔shim; no deep
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
