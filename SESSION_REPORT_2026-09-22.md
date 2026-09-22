# Session Report — 2026-09-22

Branch `feat/multi-scoring-attention-map` · **no commits** (standing rule: commit only on request) ·
final: all sessions terminated flat at 18:46 UTC

## Outcome

| | Result |
|---|---|
| Full test suite (`tests/`, excl. `test_quantconnect.py`) | **1256 passed, 1 skipped**, 0 failed (3m37s) |
| Commits | None. Everything below is uncommitted in the working tree |
| New tests | `test_entry_allocation.py` (13), `test_oversold_entry.py` (11) |
| Confidence level of today's code | Parallel sizing, alert mute, cap override and slot trim: **runtime-exercised** on QT `fba831e3` for 2.5 h (small sample, no anomalies). V4: **unit-tested only**; it has not fired in a session |

## 1. Paper trading today

| session | venue | window (UTC) | fills | net | status |
|---|---|---|---|---|---|
| `c7c25a88` | QT | 13:48–13:53 | 8 | −$165.68 | flattened on the warmup relaunch; **removed from the journals** (§6) |
| `42a5909d` | Kraken | 13:48–13:53 | 4 | −$57.56 | same; **removed from the journals** |
| `fba831e3` | QT | 13:54–18:45 | 14 | **−$271.70** | terminated: 5 positions closed, then the process ended |
| `fc48bf29` | Kraken | 13:54–18:46 | 4 | **+$75.57** | terminated: 2 positions closed; the day's only session in the black |

Net is equity minus the $100,000 start, commissions included. (The first version of this report
quoted the journal's `realized_pnl` / `unrealized_pnl` columns, which exclude commissions; it
understated the losses.)

**Where the losses came from** (15:45 UTC, all four sessions, −$595.68 at the time):

| session | commissions | spread + slippage | market move | net |
|---|---:|---:|---:|---:|
| `c7c25a88` QT | −39.60 | −99.89 | −26.19 | −165.68 |
| `42a5909d` Kraken | −19.80 | −43.01 | +5.25 | −57.56 |
| `fba831e3` QT | −19.80 | −49.95 | −222.71 | −292.45 |
| `fc48bf29` Kraken | −9.90 | −21.51 | −48.58 | −79.99 |
| **total** | **−89.10** | **−214.36** | **−292.23** | **−595.68** |

About half was trading cost, most of it the warmup relaunch: both books were sold 5 minutes after
being bought (my error, see §9). The rest was the open books' prices moving against entry. Since
15:45 both open books recovered part of that: QT from −$292.45 to −$226.04 (after ~$25 of trim
costs), Kraken from −$79.99 to **+$100.87**. Four hours of data say nothing about the strategies.

**Day total: −$419.37** across the four sessions (QT −$437.38, Kraken +$18.01).

Termination at 18:45–18:46 UTC: both sessions were stopped without flattening, then relaunched with
`--resume-session --flatten-on-exit` on top of their stop sentinel, so `run_forever` returned before
its first poll and went straight to the flatten — no new entries were opened on the way out. Every
exit routed through the Router. QT closed 5 positions (equity $99,728.30, all cash), Kraken closed 2
(equity $100,075.57, all cash). The close itself cost ~$35 in commissions plus 5 bps per fill, which
is why the final numbers are below the 18:40 marks (QT −$226.04, Kraken +$100.87).

## 2. Parallel sizing (default ON for QT and Kraken) — runtime-exercised

`risk/entry_allocation.py` + `LiveMonitor(parallel_sizing=True)`. Each poll collects every entry,
then gives free slots to the highest-conviction names, clips each to its per-symbol cap, scales all
of them by one factor if they exceed the leverage headroom, and routes them in rank order,
**advancing the position count after each accepted open**. The Router still gates every intent.

This fixes what the morning session showed: QT opened **4 positions against a cap of 3** (the
Router was handed the poll-start count for every entry), then rejected RSI.TO **68 times**.
On the new code: **0 Router rejections** in QT since 16:15 UTC (2.5 h). When there is no slot, the
allocator drops the entry before routing and says why.

## 3. Variant #4 — oversold entry (bundled with V1–V3) — unit-tested only

`signals/oversold_entry.py`, `--oversold-entry --oversold-entry-only ts_momentum`. Adds a tranche
to a **held** trend position when the last completed bar closed below the lower Bollinger band
(20, 2 sd) or RSI(14) ≤ 30, while the strategy's trend signal is on. Own ATR stop per tranche, one
per symbol per 5 bars. **Not walk-forward calibrated. Has not fired in a session.** Limits: it
never opens a position; the Router's position cap counts V4 adds as BUYs; tranche and cooldown
state is in memory only, so a restart forgets them.

## 4. Other changes

- **`--mute-alerts RSI.TO`**: skips the notification only; RSI.TO still trades (bought 1,861 at
  16:15 UTC). 11 muted alerts so far.
- **Defense names on the QT watchlist** (ITA, LMT, RTX, NOC, GD as `ts_momentum`), in response to
  the "Conflict escalation watch" thesis. No walk-forward evidence: LMT and RTX are "watch" tier
  (`reports/wf_symbols_defense_2026-09-22.md`); ITA/NOC/GD were not tested. **GD has signalled every
  poll since 16:15 (11 alerts) and gets no slot**: the book is full at 5. Same shape as the RSI.TO
  alert noise, now without Router rejections.
- **Graph journal** ran 3 polls (10,545 → 10,845 edges) and was stopped by you. One thesis firing:
  "Conflict escalation watch" (tentative, intensity 0.29).

## 5. Hard kill + resume onto the new code (16:57 UTC)

Both sessions were killed between polls (no flatten; last journal rows still showed the open
books), then relaunched with `--resume-session`. Both rehydrated exactly: QT 4 fills replayed,
cash $30.69, peak $100,065.54 carried; Kraken 2 fills replayed, cash $56,952.76. Nothing was
re-bought. **This was the first resume after a hard kill; it worked on this one occasion.**

## 6. Sessions `c7c25a88` and `42a5909d` removed from the journals

At your request, while no session was writing. Backups of every edited file are in
`state/backup_2026-09-22_pre-session-delete/`, so it is reversible.

| file | lines removed |
|---|---:|
| `paper_equity.csv` | 16 |
| `paper_fills.jsonl` | 12 |
| `paper_orders.jsonl` | 12 |
| `sizing_decisions.jsonl` | 2 |
| `intel_graph.jsonl` (fill edges) | 12 |

Their two session logs (`reports/{qt,kraken}_paper_2026-09-22.log`) were moved into the backup
folder, not deleted. They still appear in this report, in §1.

## 7. QT position cap 3 → 5, with a pro rata trim (16:15 UTC)

- `--max-positions 5` overrides `trading.yaml` for the QT process only; Kraken stays at 3.
- `--trim-to-slots` (one-shot at startup): scales the gross book to equity × held / cap. With 4 held
  and a cap of 5 that was **×0.80 on every holding**, so relative weights stayed as the sizer set
  them. The sells went through the Router into `fba831e3`'s own journal (orders 5–8), so a later
  resume replays the trimmed book.

  | | held | sold | kept |
  |---|---:|---:|---:|
  | EQB.TO | 24 | 5 | 19 |
  | QQQ | 19 | 4 | 15 |
  | SRU.UN.TO | 1278 | 256 | 1022 |
  | VDY.TO | 627 | 126 | 501 |

- On the same poll the allocator had 2 candidates for the 1 free slot. **RSI.TO** won on conviction
  and was bought at its normal size (1,861 shares, ~$12.5k, order 9); GD was dropped.
- I first built an equal-slot-notional sizing (each position = equity / N). You corrected that you
  meant the same $100k starting capital with the usual sizing, so it was removed.

## 8. Findings filed in `NEXT_SESSION.md`

- **🔴 Bug: partial sells don't book realized P&L** (Queue — correctness bugs).
  `PaperBroker._apply_fill` only books realized P&L on a full close. Equity is right; the
  `realized_pnl` column is not. On `fba831e3` the trims left **−$39.92** unbooked. Not fixed: after a
  fix, `--resume-session fba831e3…` would refuse to start (resume checks replayed realized P&L
  against the journal). Options are listed in the entry.
- **Slippage is a model, not a bug.** Every paper fill is mid + 5 bps (`slippage_bps=5.0`, fill model
  `mid`); today's $214.36 is 5 bps on ~$429k traded. The levers: fewer round trips (the no-flatten
  rule and resume already do this), a realistic `touch` fill model and the spread / open-close
  gate (both exist but only in `paper_global.py`), and limit orders.
- **Limit orders** added to Queue — build / run: the Router sends every order as MARKET and the paper
  broker ignores `limitPrice`. Scope (entries only, resting orders with TTL, re-gating, pending
  orders counted by the allocator and the cap, resume, then live) and how to judge it (fill cost
  vs mid **and** the cost of missed trades).

## 9. Mistakes made this session

- **Flattened on the warmup relaunch without asking** (−$223.24 across the two sessions, mostly
  round-trip cost). You set the rule "do not flatten without further ask"; it's saved as a
  standing memory, and every launch command now carries `--no-flatten-on-exit`.
- **Overwrote `risk/allocation.py`** without reading it first. Restored from HEAD; my code moved to
  `risk/entry_allocation.py`.
- **Ran `git stash` by accident.** Popped immediately; all 51 modified files restored, no errors in
  any running session's log.
- **Understated the day's losses** by quoting P&L columns that exclude commissions. Corrected (§1).

## Running now

Nothing. Both paper sessions were terminated flat at 18:46 UTC (books closed, then the processes
ended); the graph journal was stopped by you earlier. No Python process remains.

Both session ids stay resumable from the journals if you want to reopen the same books, but their
final rows are flat, so a resume would start from cash.

## Needs your decision

1. **The partial-sell P&L fix** and how to handle `fba831e3`'s resume check (options in NEXT_SESSION).
2. **`touch` fill model + spread / open-close gate on QT and Kraken** (offered, not built).
3. **Position-cap semantics for V4 adds** (count only new symbols, or every BUY).
4. **Defense names:** keep as unvalidated ts_momentum, walk-forward ITA/NOC/GD when no QT session is
   running, or trade them only while the thesis fires. GD's alert repeats every poll while the
   book is full; mute it too, or leave it.

## Launch commands (standing config as of 2026-09-22)

To continue the current books, add `--resume-session <id>`. `--trim-to-slots` is one-shot: leave it off.

```bash
.venv/Scripts/python.exe -m trading_live_claude.cli signal --strategy bollinger --symbols "EQB.TO,QQQ,XIC.TO,ZEB.TO,CGL.TO,VALE,DBC,SRU.UN.TO,CRT.UN.TO,ENB.TO,XIU.TO,VDY.TO,SLF.TO,RSI.TO,ITA,LMT,RTX,NOC,GD" --strategy-map "EQB.TO=ts_momentum,QQQ=ts_momentum,XIC.TO=rsi_meanrevert,ZEB.TO=atr_channel,CGL.TO=atr_channel,VALE=bollinger,DBC=bollinger,SRU.UN.TO=confirm_rsi_meanrevert,CRT.UN.TO=rsi_meanrevert,ENB.TO=confirm_bollinger,XIU.TO=composite,VDY.TO=ts_momentum,SLF.TO=bollinger,RSI.TO=bollinger,ITA=ts_momentum,LMT=ts_momentum,RTX=ts_momentum,NOC=ts_momentum,GD=ts_momentum" --interval 300 --paper --paper-equity 100000 --level --intel-overlay --profit-lock --profit-lock-exempt ts_momentum --candle-exit --candle-exit-exempt ts_momentum --overbought-exit --overbought-exit-exempt ts_momentum --oversold-entry --oversold-entry-only ts_momentum --parallel-sizing --mute-alerts RSI.TO --max-positions 5 --no-flatten-on-exit
.venv/Scripts/python.exe scripts/paper_kraken.py --interval 300 --paper-equity 100000 --parallel-sizing --no-flatten-on-exit
.venv/Scripts/python.exe scripts/graph_journal.py --iterations 96 --sleep 900 --wash-min-hours 72 --persistence-threshold 5
```

## Uncommitted changes (this session)

- New: `src/trading_live_claude/risk/entry_allocation.py`, `src/trading_live_claude/signals/oversold_entry.py`,
  `tests/test_entry_allocation.py`, `tests/test_oversold_entry.py`,
  `reports/wf_symbols_defense_2026-09-22.{csv,md}`, today's session logs in `reports/`,
  `state/backup_2026-09-22_pre-session-delete/`, this report.
- Modified: `src/trading_live_claude/monitor/live_loop.py` (parallel sizing, V4, mute,
  `trim_to_slots`), `src/trading_live_claude/cli.py` (`--oversold-entry*`, `--parallel-sizing`,
  `--mute-alerts`, `--max-positions`, `--trim-to-slots`), `scripts/paper_kraken.py`
  (`--parallel-sizing`, `--mute-alerts`), `NEXT_SESSION.md`.
- Journals edited: `state/paper_equity.csv`, `paper_fills.jsonl`, `paper_orders.jsonl`,
  `sizing_decisions.jsonl`, `intel_graph.jsonl` (§6).
