# Session Report — 2026-09-18

Branch `feat/multi-scoring-attention-map` · **no commits** (standing rule: commit only on request) ·
Kraken paper session live over the weekend

## Outcome

| | Result |
|---|---|
| Full test suite (`tests/`, excl. `test_quantconnect.py`) | **1136 passed, 1 skipped**, 0 failed (2m19s) |
| Commits | None. Everything below is uncommitted in the working tree (see "Uncommitted changes") |
| New tests | `test_thesis_intensity.py` (16), `test_profit_lock.py` (20), heartbeat/intensity/exit-reason cases in `test_notification.py`, gate pins updated in `test_intel_interpret.py` |

## 1. Paper trading today

All sessions closed flat through the Router except the Kraken session still running.

| session | venue | window (ET) | fills | net | note |
|---|---|---|---|---|---|
| `fb885ede` | QT | 09:37–11:43 | 8 | **+$229.27** | stopped by sentinel, flattened |
| `81157e7f` | Kraken | 09:37–11:43 | 2 | **−$41.47** | PAXG only |
| `d564f213` | QT | 15:00–15:27 | 8 | **−$114.10** | closed to relaunch with Variant #1 |
| `9c4aa322` | Kraken | 15:00–15:27 | 2 | **−$28.11** | PAXG ×0.75 trim (see §3) |
| `ec72c96f` | QT | 15:27–15:58 | 8 | **−$180.37** | Variant #1 on; scheduled close at 15:58 |
| `49617074` | Kraken | 15:27– | 1 | open | running (see "Running now") |

**QT day total −$65.20:** +$53.61 realized before costs, minus $118.80 of commissions
(24 fills × $4.95). The two afternoon relaunches each paid a full round trip to re-buy the same four
names for ~30 minutes; both lost after costs. The profit lock never triggered in `ec72c96f`
(SRU.UN.TO, its only covered name, never reached +1 ATR), so that session is not evidence about it.

Every QT session re-bought EQB.TO, QQQ, SRU.UN.TO and VDY.TO on its first poll and held
thereafter; ENB.TO (`confirm_bollinger`) and XIU.TO (`composite`) still have zero signals.

## 2. Futures momentum baseline: diagnosed, re-run, then removed

The −83% cross-sectional momentum QC result (2026-09-15) was reconstructed from its 2,359 orders
(tie-out to QC's net within 0.5%):

- **Roll re-entry never fired.** `SymbolChangedEvent.old_symbol/new_symbol` are strings in LEAN;
  the new-leg `market_order` never produced an order. All 834 roll fills were liquidations.
- **Fills crossed stale daily quote spreads**, worst at rolls. Fills vs submission mid cost
  $4.96M of the $8.24M loss.
- **Per-root P&L zeros** were an attribution bug (end-of-run portfolio sum), not real zeros.
- Signal contamination by roll gaps was **ruled out by reading LEAN source**, not tested.

A fixed harness (proper roll re-entry, chain-selected contracts rolled 35 days before expiry, a
spread guard, per-root attribution from fills) re-ran the same spec: **−4.7% CAGR, Sharpe −0.53,
DD 69%** (vs −9.0% / −0.84 / 85%). Roughly half the bleed was harness. **Correction logged:** I had
predicted "≈0 edge" from a biased subset and argued that "both legs losing = execution cost"; the
rerun contradicted the prediction and the argument was wrong (cross-sectional reversal also
loses on both legs).

**Then removed by your decision:** the stash, `integrations/lean_futures.py`,
`scripts/qc_futures_empirical.py` and the `qc_*` reports are deleted. The IB futures paper path
and the QC projects are kept. Parked with "refreshed approach and a more thorough baseline";
LSTM-embedded volatility-weighted momentum deferred.

## 3. Thesis alerts: why they went silent, and what changed

- **Telegram delivery works** (read-only `getMe`/`getChat` OK). The silence came from the 09-16
  recalibration: today's readings (strategic risk 67, conflict 3) sat between the old and new
  gates. Replaying today's snapshots: no theses at 73/6, both old theses on every snapshot at 60/3.
- **Gates lowered to `strategic_risk ≥ 67` / `conflict_events_active ≥ 4`** (your decision, alerts
  **and** sizing). Base rates on real snapshots ~81% / ~70%. Runtime-exercised: Complacency
  divergence fired at 14:55, and Kraken's PAXG entry was trimmed ×0.75 at 15:00 (2.859 vs 3.646
  this morning).
- **Graph-journal heartbeat** (first poll after launch, then every 24h): theses firing plus each
  reading against its gate. Runtime-exercised once (14:55); **Telegram receipt not confirmed**, so
  please check the phone.
- Correction to the 09-16 calibration: 89 of 336 overlay rows are empty/degraded, which diluted its
  base rates (the old ≥60 gate fired on 96% of *real* reads, not 75%).

## 4. Thesis intensity (display only)

`intel/thesis_intensity.py`: each firing thesis graded 0–1 = magnitude × time. Magnitude is a
bounded `log10(1+9u)` curve per input between a neutral floor and a stress ceiling (bounds from the
real-snapshot ranges); time is the same curve over 0–72h firing, with a fresh fire at 0.5 weight.
OR theses take the strongest leg, AND theses the weakest. Shown in thesis alerts and the
heartbeat (runtime-exercised: `intensity 0.27 = magnitude 0.55 × time 0.50` at 14:55). It drives
nothing. Corpus p50 ≈ 0.29–0.39, max 0.50, because recorded firing streaks never exceed 7h.

## 5. The test suite was writing into `state/`

- **Overlay journal: fixed.** Three `OverlayProvider(...)` test calls journaled fake snapshots into
  `state/intel_overlay.jsonl` on every run; they now pass `journal=False` (verified: a full run
  leaves the file unchanged). The polluted rows are left in place pending your approval.
- **Graph journal: handed to a separate session** (fake `traded` edges from paper-broker fills).
  Its fix landed in *this* working tree (`brokers/paper.py`, `tests/test_paper_broker_journal.py`),
  not in its worktree. That session has not reported back.

## 6. Exit Variant #1: profit-lock ratchet

`signals/profit_lock.py`, QT only, flag `--profit-lock` (off by default), exemption flag
`--profit-lock-exempt` (default `ts_momentum`).

- **Rule:** arms at +1 ATR; exits when price retraces from the peak by more than a giveback shrinking
  on the log curve from 3 ATR (+1 ATR) to 1 ATR (+4 ATR). The same function drives the backtest and the live loop.
- **Transaction-cost cross-check** (your request): floor at net breakeven (entry + round-trip cost);
  arms only once the gain covers 2× the round-trip cost; backtested at none / realistic / 2× costs.
- **Bug found by the cross-check:** `ts_momentum`'s `entry` is a level signal, so a lock exit was
  re-bought on the same bar (invisible and free in the backtest, a round trip per poll live).
  Fixed with a **re-entry lockout** in both paths.
- **Backtest** (`reports/profit_lock_backtest_2026-09-18.md`, in-sample, one config): lock on all
  14 halves time in market and return and "wins" sortino/DD only through the smaller drawdown.
  **Excluding `ts_momentum`: sortino/DD 45 → 65, Sortino 2.21 → 2.27, DD 4.9% → 3.5%, return
  −1.6 pp**, and it holds at 2× costs.
- **Status:** parked as Variant #1, then folded into the 15:27 QT relaunch at your request.
  Runtime-loaded, **never triggered.**

## 7. Bugs filed (NEXT_SESSION → Queue — correctness bugs)

- 🔴 `--max-prune-fraction 0.0` prunes **everything** the policy allows (`graph_journal.py:356`
  maps ≤0 → `None`; `graph.py:408` reads `None` as uncapped). Help text says 0.0 disables pruning.
- 🟡 Graph-journal wash cadence resets on every restart (in-memory `last_wash_ts`). Three washes
  today (552 + 587 + 571 edges, 5% each), and each overwrote the `.bak`.
- 🟡 QT cash goes slightly negative after first-poll buys (−$16.36, −$5.24): commissions don't
  appear to be reserved in sizing. **Not investigated.**

## Running now

**Kraken** `49617074…` (15:27 ET, `--interval 300`, warm-up done): 57 polls, 1 fill (PAXG/USD
2.686 @ $4,374.07), equity $99,995.36, unrealized +$0.31. It will run over the weekend. To stop it
cleanly: `touch state/STOP_49617074be6c499eb89cf567fd24e57e` (flattens, then exits).

QT and the graph journal are stopped.

## Needs your decision

1. **Telegram:** did the 14:55 thesis alert and heartbeat arrive? `_telegram` doesn't check HTTP
   status, so your phone is the only confirmation.
2. **State cleanup:** ~89 empty/degraded overlay rows (and fake `traded` graph edges) in `state/`.
   Removing them rewrites ground-truth files, so it needs your approval.
3. **Wash-timer fix:** persist the last wash time so relaunches stop pruning 5% each time.
4. **Open decisions #7–#8** in NEXT_SESSION (graph-weighted interpret; whether intensity drives
   anything).
5. **Commit:** 28 files changed across today's work plus other in-flight WIP; nothing staged.

## Known limits of what was built

- Profit lock: the backtest is in-sample and a single configuration with 2–43 trades per name, and
  the live lock never fired. It evaluates on polled prices live vs daily closes in the backtest, so
  live exits will trigger earlier.
- Gates 67/4 fire on most reads, so thesis alerts carry little information per fire; the dedup
  keeps volume near ~1.5 onsets/day.
- Intensity bounds are fitted to 18 days of one regime; the time dimension is under-sampled.
- The futures rerun was one config on one harness; its spread cost for next-bar fills was unmeasured.

## Uncommitted changes

**Mine (today):** `NEXT_SESSION.md`, `SESSION_REPORT_2026-09-18.md`, `scripts/graph_journal.py`,
`src/trading_live_claude/{cli.py, curves.py*, backtest/engine.py, intel/interpret.py,
intel/notification.py, intel/thesis_intensity.py*, monitor/live_loop.py, signals/generator.py,
signals/profit_lock.py*}`, `tests/{test_intel_interpret.py, test_notification.py,
test_thesis_intensity.py*, test_profit_lock.py*}`, 3 lines in `tests/test_intel_overlay.py`;
deletions of `integrations/lean_futures.py`, `scripts/qc_futures_empirical.py`,
`reports/qc_futures_empirical_2026-09-13.json` (* = new file).

**Not mine:** the desk-policy / TradeCard WIP (`desk_policy.py`, `paper_ib.py`, `paper_global.py`,
`execution/approval*.py`, `approval_card_sim.py` and their tests); `intel/routing.py` +
`scripts/paper_kraken.py` + the background-refresh tests in `test_intel_overlay.py` (edited 12:34 ET
by another session); `brokers/paper.py` + `tests/test_paper_broker_journal.py` (graph-leak session).

## Environment notes

- MCP server `interactive-brokers` failed to connect (CONNECTION_CLOSED) all session.
- `uv` not on PATH; `.venv/Scripts/python.exe` used throughout. Windows console is cp1252, so any
  script printing `→` needs `PYTHONIOENCODING=utf-8` (the `graph_journal.py --help` crash is this).
- Git Bash has no tz database (`TZ=America/New_York date` prints GMT); timed launches used UTC.

---

## Addendum — later on 2026-09-18

**Final suite: 1180 passed, 1 skipped, 0 failed.** Everything is uncommitted. All sessions ended
flat, and no process is running.

- **Exit variants (QT, off by default):** V2 bearish-candle exit and V3 overbought exit built next
  to V1. Backtest: V2/V3 on all names lose return; **standing config = V1+V2+V3 with `ts_momentum`
  exempt** (sortino/DD 45 → 69, Sortino 2.21 → 2.15, return +39% → +34%, holds at 2× costs;
  in-sample, one config). Reports: `reports/exit_variants_backtest_2026-09-18.md`,
  `reports/exit_variants_v123_backtest_2026-09-18.md`.
- **Plan written** for a calibrated entry/exit signal system on the QT book (14 names), including
  V4 "buy on oversold" as trend pullbacks. See `NEXT_SESSION.md` "Plan — robust entry/exit signal
  system".
- **Backlog pass, cheapest first.** Fixed with tests:
    * `--max-prune-fraction 0.0` and the wash timer (now persisted);
    * global `STOP` stopping only one session;
    * FX slash notation; NaN ATR; Kraken order-id stability;
    * `ib_web` confirm cap and TLS localhost guard;
    * atomic cache writes; graph append errors now logged;
    * `trading live` aborts on an env mismatch;
    * `qc-rank` filters + `sortino_over_dd` default;
    * README / `CLAUDE.md` accuracy; `state/` allow-list; `Broker.venue`.
  Verified not bugs: heat units, router journaling order. Deferred with measurements: daily-budget
  re-parse (5 ms), QT negative cash (commission not reserved).
- **Params resolver:** `--params {default,wf,calibrated}` on `cli signal`; alerts show running vs
  evidence params and flag mismatches. Default unchanged (`default`); flipping it is your call.
- **🔴 Found: `QUESTRADE_ENV` selects nothing.** The refresh token alone decides which account
  trades, so the autonomous daemon with a live token and `AUTONOMOUS_ACCOUNT=practice` would trade
  live. Options are in NEXT_SESSION; not changed.
- **TradeCard end to end (paper, Kraken):** entry and close-out both published → card ACCEPT →
  filled; passbook rows verdict ACCEPT with `broker=paper`. The fix was already in the TradeCard WIP.
- **Churn and warm-up measured:** 25 same-name re-buys across restarts; warm-up produced 0 entries
  in 16 sessions. Next session starts with journal rehydration (ordered list at the top of
  NEXT_SESSION).
