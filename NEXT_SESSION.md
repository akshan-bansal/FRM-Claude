# Next-session backlog

**Pruned 2026-09-16** to active items only. Everything removed was either shipped, run,
superseded, or closed by an explicit user decision; the full pre-prune text is in git history
(last committed version: `4d471f3`) and in the session scratchpad backup
`NEXT_SESSION.backup-2026-09-16.md`.

## Status (2026-09-17)

- Branch `feat/multi-scoring-attention-map`, last commit `4d471f3` (calibration fold), pushed.
  Nothing since has been committed. Full suite re-run 2026-09-17 after every change below:
  **1058 passed / 1 skipped / 0 failed** (3m01s; the skip is the POSIX-only signal test). Ruff
  clean on every file touched today; the 8 remaining findings are pre-existing (`cli.py`,
  `tests/test_monitor.py`, `tests/test_intel_interpret.py`).
- **Uncommitted from 2026-09-17:** `strategies/overlay.py` + `tests/test_overlay.py`
  (level-trigger strength fix, 2 regression tests); `logging_setup.py` (httpx/httpcore pinned to
  WARNING so the Telegram token stops reaching logs); `monitor/live_loop.py`, `cli.py`,
  `scripts/paper_kraken.py`, `tests/test_monitor.py` (warm-up cadence, 5 tests); this file.
- **Uncommitted from 2026-09-16:** `intel/interpret.py` + `tests/test_intel_interpret.py`
  (threshold calibration); `monitor/live_loop.py`, `cli.py`, `scripts/paper_kraken.py`,
  `tests/test_monitor.py` (flatten-on-exit, stop sentinel, sizing v2 default);
  `SESSION_REPORT_2026-09-16.md`.
- **Uncommitted from 2026-09-14** (user's WIP, not Claude's): desk-policy venue split —
  `src/trading_live_claude/desk_policy.py`, `tests/test_desk_policy.py`, `scripts/paper_ib.py`,
  `scripts/paper_global.py`. Also untracked `reports/qc_*.json`. This is the **interim** posture:
  IB carries futures/commodities, equities trade on Questrade, no IB FX, one book per currency.
  **IB is staying**, not being removed. Foreign-venue equities and IB FX are held, not dropped
  (see "Held for IB market-data subscriptions").
- **Stopping sessions:** `touch state/STOP_<session_id>` (path printed at boot). The session
  exits within one poll and flattens. Never `TaskStop` — it hard-kills and skips the flatten.
  Don't rely on the global `state/STOP` yet (bug below).
- **Sizing v2 is the Kraken default** (`--no-sizing-v2` to opt out).
- **Warm-up cadence (new 2026-09-17):** `--warmup-interval 60 --warmup-minutes 60` on both paper
  launchers polls faster for the first hour, then falls back to `--interval` in the same process
  (no restart, so no flatten). Only ever speeds polling up; 5 s floor; off unless set. Caveat:
  strategies run on daily bars, so warm-up re-checks the forming bar and live quotes — it
  re-establishes positions quickly after a flatten, it does not create new daily signals.
- **Telegram delivery is broken** — `getChat` returns "chat not found". See the item below.
- `git stash@{0}` holds the shelved futures WF harness — see "Parked".

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
   loss-exit never runs. The same unplaced stop is why the futures mirror hit a 64% DD.
5. **The QT paper book mixes CAD and USD names** without conversion (QQQ, VALE, DBC beside `.TO`).
6. **TradeCard signs neither the thesis nor the stop**, and the stop isn't displayed.
7. **Graph-weighted interpret default** — unanswered. The proposal is to keep it behind a flag,
   off, until weeks of graph polls can test whether persistence predicts anything.

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

# 5. Graph journal
.venv/Scripts/python.exe scripts/graph_journal.py --iterations 96 --sleep 900 --wash-min-hours 72 --persistence-threshold 5

# 6. Stop a session cleanly (per session — see the global-STOP bug)
touch state/STOP_<session_id>
```

Notes: `uv` isn't on PATH on this machine; use `.venv/Scripts/python.exe`. QT launched after
16:00 ET opens nothing. ARX.TO and RIG.TO were removed for 404-on-candles; pre-flight symbol
validation now catches that class of failure at launch.

## Queue — correctness bugs

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

- 🟡 **The global `state/STOP` sentinel stops only ONE session.** `_check_stop_sentinel` deletes
  the file on sight, so the first session to poll takes it. Observed 2026-09-16: Kraken
  `b5870348…` consumed it; QT `f14b7b26…` kept running until it got its own `STOP_<id>`. Fix:
  don't delete the global `STOP`. Honour it only if its `mtime` is after the monitor's start time,
  so every running session stops and later launches ignore it. Keep consuming per-session files.
  **Never touch `HALTED`.** Tests: two monitors on one directory both stop; a monitor started after
  the file ignores it; a per-session file is still consumed. Update the boot banners.

- 🟡 **Flatten-on-exit: remaining gaps.** Built 2026-09-16 (`LiveMonitor.flatten`,
  `request_stop`, `--flatten-on-exit` on `cli.py signal` and `paper_kraken.py`; stop-sentinel
  exit). Verified flat on `f082cdb1…`, `889dde54…`, `b5870348…`, `9e834d13…`. Still open:
    * Not wired into `paper_ib.py` / `paper_global.py` (the user's desk-policy WIP; coordinate).
    * `PaperBroker.__init__` starts flat with **no journal rehydration**, which is in tension with
      "`state/` files are ground truth". A crashed session's book can't be recovered or closed.
    * `_book_risk` duplicates `step()`'s inline risk block. Unify.
    * A flatten can be **rejected** by the kill-switch, the min-ticket floor or the heat cap (they
      apply to SELL). It's reported loudly but not retried. Decide the policy.
    * Orphaned books in the journals (can't be repaired; analysis must treat them as unfinished):
      `4a6ad96a`, `982b7458`, `b14e4de0`, `eeefb9e2`, `ca1252ad`, `dbefdbe2`, `e6c192ef`,
      `1af5bb80`, plus earlier sessions of the same shape.

- 🟡 **`classify_symbol` routes FX slash-notation to `crypto`** (`intel/routing.py`).
  `EUR/USD` → `crypto`; only `EURUSD` → `fx`. Knock-on: `spec_for` → wrong calibration profile
  (~60% vol vs ~9%), wrong overlay scalar, and a wrong branch for `scripts/fx_pairs_scan.py` /
  `single_fx_wf.py` (both default to slash form). Fix: check the FX shape on `BASE/QUOTE` before
  the crypto `/` check. Regression-test `EUR/USD`, `EURUSD`, `BTC/USD`, `BTC-USD`.

- 🟡 **CI runs a hardcoded test list** (`.github/workflows/test.yml`) that misses the newer
  approval, E2E, scheduler, FX, venue, monitor and interpret tests.

- 🟢 **`qc-rank` ranks a tutorial template #1** ("Adaptable Light Brown Crocodile": buys 10 TSLA
  once). Add minimum-trades / minimum-duration filters and flag runtime-errored backtests.

## Queue — build / run

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

- **Exchange hopping, levels 2–4 — HELD (decided 2026-09-16).** Not abandoned: IB stays in the
  stack, and these levels resume once market-data access exists.
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
- **Futures strategy research — shelved 2026-09-15.** Details under Parked. The market-data
  subscription is also its first revival prerequisite.

## Parked

- **Futures strategy research — shelved 2026-09-15.** Trend+reversion and cross-sectional momentum
  both failed walk-forward on QC (−1.6% and −9% CAGR; reports `reports/qc_trend_zscore_wf_2026-09-15.md`,
  `reports/qc_xsec_momentum_wf_2026-09-15.md`). The harness is in **`git stash@{0}`**
  (`git stash pop` to revive). The commodity-only universe shows no edge; the only unexplored factor
  is carry / term structure. To revive, first:
    * **Buy futures market data on IB.** Error 354 means the book would launch and never trade.
    * **Quantpedia credentials** (`QUANTPEDIA_USERNAME` / `QUANTPEDIA_API_KEY`).
    * In the LEAN mirror: enforce the sizer's assumed stop; fix per-root P&L attribution (mostly
      zeros); check MHG data coverage; add a slippage model; research a short leg and the
      international contracts.
  Futures continuous-contract history via the `ib_insync` socket (Alt B, ~3 hr) belongs here too.
- **Interlisted TSX⇄NYSE arb** (`microstructure/interlisted.py`): 0/25 pairs clear at retail FX.
  If revisited, scan during market hours with a stale-quote filter and streaming quotes.
- **Live crypto routing** through `AssetRouter`, only once the go-live decision is made.
- **Tick-aware fill prices** in `PaperBroker`, alongside tick-aware router sizing.
- **Correlation matrix snapshots** to `state/{crypto,equity}_corr.jsonl` on a schedule (needs consent).
- **Carry-inversion thesis** on a real futures-curve feed instead of the stress+flow proxy.

## Open audit gaps (from 2026-09-04, re-verified 2026-09-16)

🔴
- `monitor/live_loop.py` (~L357): `float(last.get("atr", …)) or price*0.02` passes NaN through,
  because NaN is truthy. Sizing then runs off NaN.
- `brokers/ib_web.py` (~L503): the `place_order` auto-confirm `while` loop has **no cap**. In live
  mode it accepts warnings a human should see, and it could loop forever.
- `README.md:141`: shows `EXECUTION_MODE=live …`, which contradicts the CLAUDE.md non-negotiable.
- `cli.py` (~L1548): `os.environ["QUESTRADE_ENV"] = …` is set after settings load, so the override
  does nothing. Compounded by `@lru_cache` on `get_settings()` (`config/settings.py`).

🟡
- `brokers/kraken.py::_txid_to_int` uses `hash()`, which is randomized per process, so order ids
  change across restarts.
- `execution/daily_budget.py::snapshot` re-parses the whole `orders.jsonl` on every gate check.
- `data/cache.py::put` writes without an atomic rename. `cache.py` paths carry no schema version,
  and `data/market.py` has no incremental tail fetch.
- `monitor/live_loop.py` (~L553): `heat = existing_risk / equity` is dollars vs fraction when the
  hedge is on.
- `brokers/models.py`: the `Fill.venue` Literal predates multi-venue.
- `execution/asset_router.py`: `AssetClass` has drifted from `OverlayClass`, and its brokerages
  map to LEAN names, not the real adapters.
- `intel/apply.py::apply_overlay` has no runtime caller.
- `brokers/base.py`: the `Broker` Protocol doesn't declare `venue`.
- `cli.py` `trading live` warns but doesn't abort when `QUESTRADE_ENV != "live"`.
- `brokers/ib_web.py`: `verify_ssl=False` with no guard that the host is localhost.
- Missing tests: `data/market.py`, `data/cache.py`, `execution/journal.py`, the futures pipeline,
  `microstructure/*`, `intel/{apply,chart,worldmonitor}.py`, several scripts.

🟢
- The README strategy table is stale (12 example files now). The README and CLAUDE.md disagree on
  the agent set, and CLAUDE.md's entry-points list is stale.
- `PaperBroker._order_counter` is a class variable, shared across instances.
- `brokers/token_store.py` uses a fixed PBKDF2 salt.
- `pyproject.toml`: dependencies have no upper caps; `ib_insync` is unmaintained (consider
  `ib-async`); PyJWT isn't declared.
- `brokers/questrade.py`: `LOGIN_HOST` is hardcoded.
- Older journal rows lack `session_id`, so joins must tolerate NULL.
- The router journals an intent before the kill-switch check, which duplicates rows when halted.
- `intel/graph.py::append_edges` swallows all errors, so a full disk drops writes silently.
- `starting_equity` and `_INTERPRET_BIAS_FLOOR` are hardcoded rather than configurable.

## TradeCard (§11) — open work

**Architecture (decided 2026-09-08):** zero vendor infrastructure. Releases go through GitHub
(sigstore-signed), and the PWA and docs through GitHub Pages. Push goes via a user-chosen relay. The
shim is single-owner-per-box, with no accounts or telemetry. See memory
`tradecard-zero-vendor-infra-architecture.md`. Branch `feat/tradecard-approval` is pushed; the
**PR is not opened** (`gh` isn't installed — use the GitHub web UI).

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
