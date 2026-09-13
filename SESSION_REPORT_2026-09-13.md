# Session Report — 2026-09-11 → 2026-09-13

Branch `feat/multi-scoring-attention-map` · 3 commits, not pushed · Kraken paper session live

## Outcome

| | Start | End |
|---|---|---|
| Full test suite | 887 passed, **5 failed** | **987 passed, 0 failed** (1 POSIX-only skip) |
| Standalone E2E runner | 4 passed, 2 failed | 6 passed, 0 failed |
| New lint / type errors on touched files | — | none (checked against a clean `HEAD`) |

| Commit | What |
|---|---|
| `400a6ae` fix(tradecard) | E2E audit findings on the card approval path |
| `8042c91` feat(paper) | Stale-quote guard on paper feeds |
| `2dd2043` feat(exchange-hopping) | 24h global book across venues in one CAD numeraire (levels 1–4) |

## 1. E2E test suite + audit

Full write-up: `E2E_AUDIT_2026-09-11.md`.

- The "production-ready, 8/8 passing" sign-off came from `test_e2e_isolated.py`, which imported **no production code**. It is renamed `test_e2e_isolated_mocks.py`, and `E2E_TEST_RESULTS.md` is marked superseded.
- **Fixed:**
  - Overlay risk clauses (geo-risk, energy, fear/greed) were dropped from the card thesis when it was truncated.
  - The card displayed JSON fields but signed separate bytes. It now renders only what it signs.
  - A second response racing the first could flip a verdict. Consumption is now atomic in both stores.
  - Fractional crypto sizes were signed as `0` shares.
  - Kraken order volumes were sent as `5e-08`.
  - The physical card could never authenticate to the shim.
  - The card's receive buffer overflowed silently at about 5 queued prompts.
  - Three broken tests had meant the accept path was never exercised.
- Firmware changes are **not compiled** (no ESP-IDF on this machine).

## 2. Stale-quote guard

`FreshQuoteBroker` flags quotes that are halted, delayed, priceless, crossed, or unchanged for 900 s. Kraken and IB don't timestamp their quotes, so the unchanged check is the only way to see those feeds stall. A stale quote:
- rejects that symbol's paper fill, and the rejection is journaled;
- skips only that symbol in the monitor;
- leaves held positions valued at their last good price, instead of $0 in the heat gate.

It is wired into every paper entry point, but not the live or autonomous paths.

## 3. Exchange hopping (levels 1–4)

- **Venues:** `trading_live_claude/venues.py` is the single table of suffix → IB exchange, currency, hours, board lot. It covers US, TSX, TSX-V, LSE, ASX, Tokyo and Hong Kong, including lunch breaks and daylight saving.
- **L1:** closed venues aren't polled.
- **L2:** IB routing uses the table. This fixed IB socket quotes and candles, which were hardcoded to US/USD.
- **L3:** paper prices convert to **CAD** using IB spot FX routed through USD.
- **L4:** `SessionRouter` queues intents for closed venues. At the next open (plus a 5 min buffer) each is re-priced and re-gated. A queued intent expires after its TTL or if the price gaps through its stop. `scripts/paper_global.py` runs IB equities and Kraken crypto in one book, with one kill-switch.
- **Microstructure** (live quotes and exchange rules only):
  - auction buffers;
  - a spread ceiling on entries (exits are never blocked);
  - board-lot rounding after size trims (Hong Kong needs `board_lots` set);
  - bid/ask fills;
  - date-aligned returns with Dimson lead-lag correlation.

## Running now

- **Kraken paper**, session `d8a0eca4…`, started 2026-09-13 16:51, log `logs/paper_kraken_2026-09-13_1651.log`. After 7 polls: 0 orders, 0 fills, no errors. It was started before the exchange-hopping commit, so it runs the earlier code.

## Needs your decision — ranked

1. **IBKR OAuth token is in pushed git history** (`NEXT_SESSION.md`, commit `638f8d3`, on both remote branches). Rotate it; deleting the line won't remove it from history.
2. **The kill-switch is at 8%, not 3%.** `config/trading.yaml` has `max_drawdown_kill_switch: 0.08`, which overrides the 3% default recorded as landed on 09-08.
3. **The real-money paths skip the intel gates.** `trading live` (`cli.py`) and `autonomous_run` build the monitor without the overlay, interpret, allocator or persistence gates. They also lack the stale-quote guard and session routing.
4. **The intra-day forced exit never runs.** `Router.check_forced_exits` is only called from tests.
5. **PAXG signal never sized.** The entry signal fired on 7 straight polls with `sized: 0`, so no order was placed, yet a Telegram alert went out on each poll. The cause is not yet diagnosed; one candidate is whole-unit flooring on a ~US$4,300 instrument. The allocator's ×3.90 boost on BTC/PAXG may therefore never apply.
6. **The Questrade paper book mixes CAD and USD** (QQQ, VALE, DBC alongside `.TO` names). That book has been mis-scaled all along.
7. **Leverage-cap trimming floors crypto to zero** (`router.py:293`, `int(fit_notional // entry)`).
8. **Thesis, stop-loss and risk dollars are not signed by the card**; the stop isn't displayed either.

## Known limits of what was built

- **Level 3 over IB's web transport:** it can't convert FX. `paper_ib` over web refuses non-CAD symbols; use `--transport socket` or `--account-currency USD`. `--futures` is web-only, so it needs USD.
- **Exchange holidays** aren't modelled; the stale-quote guard covers them.
- **London:** LSE instruments quoted in pence would come out 100× off. Verify one quote on TWS before trading London.
- **Correlations** use native-currency returns; FX isn't in the covariance.
- **The scheduler's queue is in memory.** A restart drops it, and signals re-queue on the next poll.
- **Shared state:** `paper_global.py` shares `state/` (including the `HALTED` sentinel) with the other paper sessions. It has no card, news or allocator.
- **Before trading Asia or Europe:** IB market-data subscriptions, and a walk-forward run per symbol (your collect-data-first rule).

## Environment notes

- `uv` is not on PATH; use `.venv/Scripts/python.exe`. `pytest-timeout` is not installed.
- No C compiler or ESP-IDF, and no IB Gateway/TWS listening during the session.
- CI (`.github/workflows/test.yml`) runs a hardcoded file list that omits the new approval, E2E, scheduler, FX and venue tests.
- `NEXT_SESSION.md` is stale. §11 and the audit-gap statuses predate this session.
