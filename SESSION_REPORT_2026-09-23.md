# Session Report — 2026-09-23

Branch `feat/multi-scoring-attention-map` · last commit `114b1aa` (yesterday's work) ·
state as of 14:50 UTC: QT, Kraken and the graph journal all running

## Outcome

| | Result |
|---|---|
| Full test suite (`tests/`, excl. `test_quantconnect.py`) | **1258 passed, 1 skipped**, 0 failed (5m01s, one sleeve test updated for the PAXG drop) |
| Commits today | None. `114b1aa` was yesterday; everything below is uncommitted |
| New tests | `close_symbols` (2 cases, in `test_entry_allocation.py`); `test_crypto_sleeve` updated |
| Confidence | Parallel sizing: **runtime-exercised** — 5 entries allocated and routed in one poll at the open, 0 Router rejections. V4: **still has not fired** |

## 1. Running sessions

| session | venue | since (UTC) | fills | equity | note |
|---|---|---|---|---|---|
| `94ea903d` | QT | 13:27 | 5 | **$99,826.53** (−$173.47) | cap 6, 19 symbols, peak $100,094.01 |
| `bc361280` | Kraken | 14:03 | 1 | **$99,989.04** (−$10.96) | 12 pairs, fresh after the flatten |
| — | graph journal | 13:28 | — | — | iter 6/96, 13,232 edges (+909) |

**QT `94ea903d`** opened its whole book in the **first poll after the open** (13:30:39): VDY.TO 812,
QQQ 13, SRU.UN.TO 622, RSI.TO 1342, EQB.TO 14. Cash $353; positions $99,473.

**This is the first clean runtime test of parallel sizing.** The allocator logged
`candidates=5, slots=6, budget=$100,000, routed=[all 5]` — five entries decided together against
one book, every one sized and accepted, **zero Router rejections**. The same five signals on
yesterday's sequential path produced the 4-positions-against-a-cap-of-3 overshoot and 68
consecutive RSI.TO rejections. RSI.TO is now held, not rejected, and its alerts are muted as asked.

**Kraken `bc361280`** holds LINK/USD 14.55 (~$185). Sizes are small because the live intel overlay
is trimming crypto conviction to **×0.445** on current readings.

**Graph journal:** Complacency divergence (moderate, 0.27) and Conflict escalation watch
(tentative, 0.33); strategic-risk index 67/100, exactly on its gate.

## 2. PAXG dropped from the Kraken sleeve (your decision)

Rationale as you put it: the gold move it was meant to diversify into has already happened, so it
was carrying cost without the diversification case.

- **Removed** `PAXG/USD` from `CRYPTO_SLEEVE` (`analysis/universe.py`), with a dated comment and the
  one-line snippet to restore it. Sleeve is now 12 pairs.
- **New `--close-symbols` on `paper_kraken.py`** + `LiveMonitor.close_symbols()`: closes named
  positions in full at startup, through the Router, into the session's own journals, so
  `--resume-session` replays a book without them. Needed because **a symbol removed from the
  watchlist is never evaluated again** — its position would otherwise sit in the book with no exit
  path. One-shot flag; not on the standing command.
- The first Kraken session of the day (`d2015d4b`) had already bought PAXG 5.53 (~$23.9k) 90 s
  after launch. It was stopped without flattening, then resumed with `--close-symbols "PAXG/USD"`:
  sold 5.53 @ 4305.25. **The PAXG round trip cost $58.21.**
- PAXG stays a safe-haven exemplar in `intel.interpret.THEME_EXEMPLARS` (risk overlay, not a
  trading list), and `brokers/kraken.py` still maps the pair.

## 3. Kraken flattened and relaunched (your instruction)

`d2015d4b` was stopped, flattened (LINK/USD 9.41 @ 12.7229) and ended flat at **$99,922.16
(−$77.84**, of which $58.21 was the PAXG round trip). The fresh session `bc361280` started from
$100,000 with the 12-pair sleeve.

## 4. Filed in `NEXT_SESSION.md` (both uncommitted)

- **Intel thesis layer — fix the inputs and the gates before adding theses.** Your question was
  whether to build new theses on the 8, given only 2 fire. Answer recorded as: not yet. Four of the
  eight are gated on values the feed has never reached (fear/greed ≥70 or ≤25 against an observed
  54–68 and zero fires in 280 reads; disasters ≥5 against a max of 2; a `disaster_accel` key absent
  from every payload; accel ≥2.0 against a max of 1.20), and the two that do fire are near-constants
  (81% / 70% of real reads). Ordered plan: (1) fix degraded-row detection — 89 of 336 rows are empty
  yet flagged `degraded: false`; (2) trailing-percentile gates instead of hard constants, which is
  what actually fixes "only 2 fire" and survives a WorldMonitor recentring; (3) new inputs — VIX via
  IB, **VIX term-structure inversion**, **credit spreads** (HYG/LQD/IEF, Questrade-fetchable), and a
  **cross-asset correlation spike** from the RMT work; (4) score the layer before expanding it.
- **Wire the RMT eigen representation into the live analysis layers.** Your question: is the
  eigenvalue representation in the top analysis layers? **No** — `analysis/rmt.py` has one importer,
  `scripts/rmt_lstm_study.py`, and every correlation the live path uses is a raw sample estimate
  (heat gate, allocator bias; the new entry allocator uses none). Three entry points, ranked:
  (1) denoise the heat gate's correlation matrix — cheapest, one call site, highest value;
  (2) eigen-aware entry ranking in the parallel allocator, so two names on the same dominant mode
  don't take both free slots; (3) top-eigenvalue share as a thesis input — last, because that layer
  is unscored. Caveat recorded: MP denoising has its own parameters and the σ² estimate already
  collapsed 1 → 10 signal modes on crypto once, so it is a sizing change, not a drop-in.

## 5. Still open from yesterday

1. **Partial-sell realized-P&L bug** (🔴 in the correctness queue) and how to handle the resume
   cross-check. Note `--close-symbols` sells in full, so it books realized P&L correctly.
2. **`touch` fill model + spread / open-close gate** on QT and Kraken (offered, not built).
3. **Position-cap semantics for V4 adds** (count only new symbols, or every BUY).
4. **Defense names** — ITA, LMT, RTX, NOC, GD are on the QT watchlist with no walk-forward
   evidence. None of them signalled today; GD's alert repetition stopped once the book had room.

## Uncommitted changes

- `src/trading_live_claude/monitor/live_loop.py` — `close_symbols()`.
- `scripts/paper_kraken.py` — `--close-symbols`.
- `src/trading_live_claude/analysis/universe.py` — PAXG/USD out of `CRYPTO_SLEEVE`.
- `tests/test_entry_allocation.py` — 2 `close_symbols` cases; `tests/test_crypto_sleeve.py` — sleeve
  list updated and an explicit "PAXG not in sleeve" assertion.
- `NEXT_SESSION.md` — the two queue entries above.
- Plus yesterday's other untracked artifacts (`reports/` logs, `SESSION_REPORT_2026-09-2{2,3}.md`).

## Launch commands in use today

```bash
.venv/Scripts/python.exe -m trading_live_claude.cli signal --strategy bollinger --symbols "EQB.TO,QQQ,XIC.TO,ZEB.TO,CGL.TO,VALE,DBC,SRU.UN.TO,CRT.UN.TO,ENB.TO,XIU.TO,VDY.TO,SLF.TO,RSI.TO,ITA,LMT,RTX,NOC,GD" --strategy-map "EQB.TO=ts_momentum,QQQ=ts_momentum,XIC.TO=rsi_meanrevert,ZEB.TO=atr_channel,CGL.TO=atr_channel,VALE=bollinger,DBC=bollinger,SRU.UN.TO=confirm_rsi_meanrevert,CRT.UN.TO=rsi_meanrevert,ENB.TO=confirm_bollinger,XIU.TO=composite,VDY.TO=ts_momentum,SLF.TO=bollinger,RSI.TO=bollinger,ITA=ts_momentum,LMT=ts_momentum,RTX=ts_momentum,NOC=ts_momentum,GD=ts_momentum" --interval 300 --paper --paper-equity 100000 --level --intel-overlay --profit-lock --profit-lock-exempt ts_momentum --candle-exit --candle-exit-exempt ts_momentum --overbought-exit --overbought-exit-exempt ts_momentum --oversold-entry --oversold-entry-only ts_momentum --parallel-sizing --mute-alerts RSI.TO --warmup-interval 60 --warmup-minutes 60 --no-flatten-on-exit --max-positions 6
.venv/Scripts/python.exe scripts/paper_kraken.py --interval 300 --paper-equity 100000 --parallel-sizing --warmup-interval 60 --warmup-minutes 60 --no-flatten-on-exit
.venv/Scripts/python.exe scripts/graph_journal.py --iterations 96 --sleep 900 --wash-min-hours 72 --persistence-threshold 5
```

Stop without selling: `touch state/STOP_<session_id>` (read at the next poll).
