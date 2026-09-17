# Session Report — 2026-09-15 → 2026-09-16

Branch `feat/multi-scoring-attention-map` · 1 commit, pushed · Kraken + QT paper sessions live

## Outcome

| | Result |
|---|---|
| Full test suite (`tests/`, excl. `test_quantconnect.py`) | **1028 passed, 1 skipped**, 0 failed (3m13s) |
| Commit | `4d471f3` — pushed to origin |
| PR | **Not opened** — `gh` CLI not installed on this machine |

| Commit | What |
|---|---|
| `4d471f3` feat(calibration) | Fold 2026-09-15 WF-sweep winners into class-specific overrides |

## 1. Calibration signal-statistics sweep (the queued item)

Ran the sweep queued under "Asset-class calibration — signal-statistics follow-up" for the
equity + crypto slice. FX was skipped: its deep-history fetch is itself still queued.

- **Harness:** `scripts/calibration_sweep.py` (new). Walk-forward with parameters **pinned** —
  unlike `sweep_universe.walk_forward`, which re-optimizes per fold. Pinning is the point: the
  OOS surface has to be attributable to the specific parameter value, not to whatever the
  re-opt picked. Scored on `sortino_over_dd`, windows from `WF_PROTOCOLS` per class.
- **Universe:** equity `EQB.TO, QQQ, XIC.TO, ENB.TO, VDY.TO` (n=5); crypto `XBTUSD, ETHUSD,
  PAXGUSD` (n=3 — the only pairs with `_daily.parquet` built).
- **Output:** 120 rows in `reports/calibration_sweep.csv`, winners table in
  `reports/calibration_sweep.md` (gitignored — see §5).

Winners by median OOS across the basket:

| class | strategy | param | winner | median OOS | WFE |
|---|---|---|---|---|---|
| equity | `bollinger` | `n_std` | 1.5 | 6.66 | 1.24 |
| equity | `zscore_ou` | `entry_z` | 1.5 | 6.86 | 1.03 |
| equity | `rsi_meanrevert` | `oversold` | 35 | 3.67 | 1.00 |
| crypto | `rsi_meanrevert` | `oversold` | 35 | 2.28 | 1.54 |
| crypto | `bollinger` | `n_std` | 1.5 | 3.00 | **0.35** |
| crypto | `zscore_ou` | `entry_z` | 0.5 | 6.16 | **6.20** |

## 2. The fold — and why it is partial

`analysis/calibration.py` gains `_apply_sweep_overrides(strategy_name, asset_class, kwargs)`,
applied in `calibrated_kwargs` *after* the heuristic calibrator runs. It replaces only the
signal-statistics axes (`n_std` / `oversold` / `entry_z`). Time-scale axes (`window`,
`exit_ma`, `atr_window`) keep the half-life formulas the calibration matrix already validated.

**Fold policy: WFE ≥ 1.0 only.** Four of six winners landed. The two crypto cells that did not:

- `bollinger n_std` — WFE 0.35, i.e. ~65% OOS/IS degradation on a 3-name basket.
- `zscore_ou entry_z` — WFE 6.20, a degenerate ratio (IS score near zero), so WFE carries no
  information as a confidence signal.

Both are documented in-code as `SEEN, NOT FOLDED` with their numbers, so the evidence is
discoverable without re-running the sweep and without silently trusting a shaky median.

A first pass folded all six. That broke
`test_bollinger_calibration_widens_bands_for_high_vol_assets`, which asserts crypto bands are
wider than equity — an a-priori design claim the sweep contradicts. Retrofitting the test to
match a WFE-0.35 finding would have been backwards, so the crypto cell stayed on the heuristic
and the test passes on its original semantics.

## 3. 🔴 The live paper path runs stock class defaults

Found while checking whether §2's fold reaches the live path. It does not — **and neither does
walk-forward.** Verified by direct inspection:

- `cli.py:132` — `_strategy_or_die(name)` returns `STRATEGIES[name]()`, zero-arg.
- `cli.py:250` — the `--strategy-map` build calls it for every symbol.
- `analysis/calibration.py::calibrate_for` has exactly one caller in the repo: `tune.py:130`.
- `intel/notification.py:88` — the alert's parameter line renders `wf_record.params` from the
  `WALK_FORWARD_VALIDATED` registry, never the live instance.

Measured on 3 symbols from today's QT session `b14e4de0…`:

| symbol | alert claimed | actually running |
|---|---|---|
| `SRU.UN.TO` | `window=7, oversold=25` | `window=14, oversold=30` |
| `VDY.TO` | `lookback=63, threshold=0.02` | `lookback=126, threshold=0.0` |
| `EQB.TO` | `lookback=126, threshold=0.0` | same — by coincidence only |

**Consequence:** every WF-evidence block in every paper alert to date describes a configuration
that may not be the one that traded. `state/paper_fills.jsonl` is therefore not evidence about
WF-validated configs, and earlier reasoning that treated it that way needs re-examining.

Sample is 3 of 14 symbols. The blast radius across the full pool is **unmeasured**.

## 4. Filed to NEXT_SESSION

- 🔴 the bug in §3, with the verification trail and the measured divergence table.
- The A/B plumbing request, respecced. Taken literally, "WF-validated params pull from
  `calibrated_kwargs`" would overwrite per-symbol earned evidence with class-level medians and
  recreate §3's misreporting bug in a new place. Proposed instead: fix the reporting bug, add
  one `resolve_params(strategy, symbol, mode)` precedence chain, then a
  `--params {wf,calibrated,default}` flag so the A/B has an honest baseline arm.
- FX slash-notation misclassification (🟡) — `classify_symbol` checks `/` for crypto before the
  6-letter FX check, so `EUR/USD` resolves to `crypto`. `scripts/fx_pairs_scan.py` and
  `scripts/single_fx_wf.py` both default to slash notation.
- Runtime-exercise gap on `composite` / `confirm_*` — registered, unit-tested, zero paper fills.

## Running now

Both started 2026-09-16 14:02 UTC, `--interval 300`, $100k paper equity, intel overlay on.

**QT** `b14e4de0…` — 11 polls. 4 fills on the first poll, none since; every later poll HOLD.

| symbol | qty | fill | notional |
|---|---|---|---|
| EQB.TO | 21 | $127.18 | $2,671 |
| QQQ | 13 | $709.77 | $9,227 |
| SRU.UN.TO | 1615 | $26.88 | $43,409 |
| VDY.TO | 1293 → **578** | $77.32 | $44,690 |

Equity $99,832.90, unrealized −$147.30, realized $0, drawdown 0.167%. VDY.TO was trimmed 55% by
the router size cap — same name trimmed 87% in the 2026-09-15 session; the milder cut tracks a
looser overlay (equity scalar 0.504 today vs 0.410 yesterday).

**Kraken** `eeefb9e2…` — 10 polls, 3 fills: PAXG/USD 2 @ $4,337.06, XRP/USD 8320 @ $1.2717,
SOL/USD 15 @ $97.01. Equity $99,965.08, unrealized −$20.07, drawdown 0.035%. Note PAXG filled
here, where the 2026-09-15 session had it stuck at `sized: 0` for 13 consecutive polls.

Both are **default-params arm** data per §3.

## Needs your decision

1. **§3 is a correctness bug on a path that produces evidence you act on.** It should probably
   jump the queue ahead of the A/B work that depends on it.
2. **PR** — `gh` is not installed. Either `winget install --id GitHub.cli` and I retry, or open
   https://github.com/akshan-bansal/FRM-Claude/pull/new/feat/multi-scoring-attention-map
3. **Crypto basket for the sweep is n=3.** Widening it needs `_daily.parquet` for the other 10
   sleeve pairs. Until then both crypto WFE numbers stay untrustworthy.

## Known limits of what was built

- The sweep is one run on thin baskets (n=5 equity, n=3 crypto). It shows direction, not enough
  to bind class constants with confidence.
- The fold is **code-complete and unit-tested. It has never executed on a live path** — by §3,
  `calibrated_kwargs` is unreachable from the paper monitors. Today's fills say nothing about it.
- RSI `oversold=35` won at the top edge of the `{20,25,30,35}` grid in both classes. The grid
  should be extended to `{40,45}` before treating 35 as a maximum.
- `scripts/screen_futures.py` (from 2026-09-10) is still unrun — it needs TWS on 7496.

## Environment notes

- `gh` CLI absent; no VS Code / Cursor / Windsurf / Zed installed, so `open_in_editor` fails.
- `uv` is not on PATH — use `.venv/Scripts/python.exe` directly.
- MCP server `interactive-brokers` failed to connect (CONNECTION_CLOSED) for this whole session.
- Working tree carries an unrelated in-progress "desk venue split" (`scripts/paper_ib.py`,
  `scripts/paper_global.py`, `src/trading_live_claude/desk_policy.py`,
  `tests/test_desk_policy.py`). Not authored here and deliberately left unstaged.
