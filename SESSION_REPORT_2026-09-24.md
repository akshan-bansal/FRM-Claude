# Session Report — 2026-09-24

Branch `feat/multi-scoring-attention-map` · last commit `b1cdabd` (not this session's) ·
state as of 15:12 UTC: QT and Kraken running, graph journal stopped by the user

## Outcome

| | Result |
|---|---|
| Code written today | **None.** Yesterday's uncommitted work is unchanged (65 modified tracked files) |
| Sessions | QT `af9d28bc` (3 fills), Kraken `3a61c0a9` (0 fills), graph journal 3 polls |
| First for this repo | The QT book is running **`--params wf`** — walk-forward registry parameters, not stock class defaults |

## 1. `--params wf` — the QT book finally runs the configs its alerts cite

This closes the 🔴 red item "the live paper path runs STOCK CLASS DEFAULTS, not WF-validated or
calibrated params, and the alerts misreport this" (open since 2026-09-16). The resolver was built
on 09-18 but its default was left at `default` pending your call; today you asked for
walk-forward-validated configs, so the flag is on.

Where each symbol's parameters came from, per the boot banner:

| source | n | symbols |
|---|---:|---|
| **WF registry** | 10 | EQB.TO, QQQ, XIC.TO, ZEB.TO, CGL.TO, VALE, DBC, CRT.UN.TO, VDY.TO, SLF.TO |
| **calibrated** | 3 | SRU.UN.TO, ENB.TO (the `confirm_*` pair), RSI.TO |
| **defaults** | 6 | XIU.TO (`composite`), and ITA/LMT/RTX/NOC/GD — the defense names have no evidence of any kind |

Real changes from yesterday's defaults: QQQ `lookback 126 -> 189, threshold 0.0 -> 0.02`,
VDY.TO `lookback 126 -> 63, threshold 0.02`, XIC.TO `window 14 -> 7, oversold 30 -> 35`,
SLF.TO `window 10, n_std 1.5`, VALE `window 15, n_std 2.5`. **The MISMATCH warning that appeared on
every entry alert is gone** — the running config and the cited evidence now agree.

## 2. What that changed in behaviour

| | 2026-09-23 (defaults) | 2026-09-24 (`wf`) |
|---|---|---|
| entry candidates, first poll | 5 | **3** |
| book invested | ~100% | **24%** ($24,375 of $99,886; $75,511 cash) |
| largest position | VDY.TO 63% | RSI.TO 11% |
| VDY.TO | dominant position both sessions, −$557 and −$203 | **did not signal** |

Fills: QQQ 15 @ 737.80 ($11,067), RSI.TO 1651 @ 6.7584 ($11,158), EQB.TO 18 @ 124.92 ($2,249).
Zero Router rejections; the allocator routed all three of its candidates (`candidates=3, slots=6`).

Two changes are confounded here, so **neither is isolated**: the walk-forward parameters AND
yesterday's position-cap clamp (no name above the flat 50%) are both live for the first time. The
smaller book is consistent with fewer signals rather than with the clamp — the largest position is
11%, nowhere near the old 75% ceiling, so the clamp did not bind on this poll.

**QT equity:** opened $99,995.05, peak $100,014.92 at 14:21, trough $99,885.56 at 15:07, now
**$99,886.47 (−$113.53)**, all unrealized. 61 polls, 59 muted RSI.TO alerts. V4 has still never
fired.

## 3. Kraken `3a61c0a9` — 60 polls, zero trades

Not an error: **no pair produced an entry signal all session** (zero allocation events, zero sizing
rows, every symbol HOLD each poll). Yesterday's LINK entry was an event-triggered channel break
that has not repeated. The sleeve runs its own screened parameters; `paper_kraken.py` has no
`--params` equivalent, and there is no walk-forward registry for crypto — the sleeve is explicitly
tier `screened`, so "walk-forward configs" applies to the QT book only.

**Journaling quirk worth knowing:** a session that never trades writes **no rows at all** to
`paper_equity.csv` (`PaperBroker` journals equity only when there are fills, positions or realized
P&L). So a flat session looks identical to a dead one in the journals — which is exactly how this
one first appeared in the status check. The log is the only evidence it was alive.

## 4. Graph journal

3 polls (14,482 -> 14,883 edges, +401), then stopped by you. Overlay scalars at 10:38 UTC:
equity 0.589, futures 0.500 — both looser than yesterday's 0.445 crypto trim.

## 5. Carried over, unchanged

Uncommitted from 2026-09-23: `close_symbols` + `--close-symbols`, the PAXG sleeve drop, the
**position-cap clamp** (`Router._position_cap_pct` now takes `min(dynamic, flat 50%)`), their
tests, and five `NEXT_SESSION.md` queue entries — cost-aware position sizing, the feed diff, the IB
equity migration, RMT wiring, and the thesis layer. Test suite as of yesterday's last run: 1286
passed, 1 skipped.

**Open, unchanged:** the partial-sell realized-P&L bug (🔴); `touch` fill model + spread gate;
V4 adds counting against the position cap; the defense names' lack of evidence; and the
`base_pct` question on the vol-scaled cap — you chose to wait for a case where two calm names both
sit at the 0.50 ceiling before changing it.

## Running now

- QT paper `af9d28bcc0ce4682b33928dc91251ab0` — `--params wf`, cap 6, warmup done, flatten-on-exit OFF.
- Kraken paper `3a61c0a9fa9646409bf3489e6596e02e` — 12 pairs, flat, flatten-on-exit OFF.
- Graph journal: stopped.

Stop without selling: `touch state/STOP_<session_id>`. Terminating (close then stop) is two steps:
stop with the sentinel, then relaunch `--resume-session <id> --flatten-on-exit` with the sentinel
in place, which flattens before the first poll.
