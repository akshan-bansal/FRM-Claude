# OS — Investment Intelligence Dashboard

One page: **`TradeCard Desk Panel.html`**, sized for the 8" 1280×800 touch panel. Four tabs.

| Tab | What it shows | Where the numbers come from |
| --- | --- | --- |
| APPROVALS | Card prompts, verdicts, response time, signature coverage | `state/approval.db` |
| BOOK | Equity and drawdown, realised P&L, round trips, gate denials, when the book trades | `state/paper_equity.csv`, `paper_fills.jsonl`, `orders.jsonl`, `rejected.jsonl` |
| INTEGRITY | Ledger chains, intent fingerprints, what the record does not prove | `state/ledger/` |
| ATLAS | The 120-meter evidence catalogue, folded in from the standalone Meter Atlas page | `meter_catalog.csv` + whatever snapshot you load |

## Rebuilding it

The page is generated, not hand-edited — every figure is baked in at build time, because the panel is
served static to a device on the LAN with no backend of its own. Edit
`pwa/desk.template.html` in the repo, never this HTML file, then from the repo root:

```bash
python scripts/build_desk_dash.py
```

That writes the same bytes to `pwa/desk.html` and to `TradeCard Desk Panel.html` here, so the two
cannot drift. It also prints what it excluded and why.

Data that cannot be represented faithfully is left out rather than annotated: sessions that were
killed with positions still open report `realized_pnl = 0.00` for ever, so they are filtered out of
every aggregate and the remaining scope is stated on the page.

## Live feedback from a running session

The file above is the last build. To watch trades as they happen, open the panel **from the session
that is running**: whatever boots the card shim also serves this page.

```bash
python scripts/paper_kraken.py --interval 20 --require-card --card-shim-port 8787 --card-ttl 120
```

```bash
uv run trading signal --strategy bollinger --symbols XIC.TO --paper --require-card --card-shim-port 8787
```

Either one prints `desk panel LIVE at http://127.0.0.1:8787/desk`. Open that. The page then reads
the session's own approval store over `/v1/intents/pending` and `/v1/stats`: the queue fills as the
router puts intents in front of the card, a prompt takes the screen with its fingerprint and TTL,
and a LIVE SESSION strip shows equity, drawdown, pending and decided counts and the card's measured
response time. A figure the shim cannot source is left blank and named, never substituted.

Reaching the panel from the 8" device means binding an interface it can see —
`--card-shim-host 192.168.x.x` — which is a deliberate choice, not the default. If the shim was
started with a token, the page asks for it once per browser tab; it is never put in the URL.

**The panel is read-only.** It shows the prompt and its fingerprint so you can compare them against
the card's screen. The ACCEPT that decides an order is a signature made on the card — that is the
whole point of the card, and a dashboard button cannot stand in for it.

Without a link the page shows no queue at all, and says so. It does not draw a specimen prompt.

## The ATLAS tab

The catalogue states what the code **defines**. A meter with no measurement is drawn neutral, with no
needle, and no value is substituted for it — that is the point of the tab, not a gap in it.

Readings arrive only from a snapshot. To take one from the journals:

```bash
python "OS-InvestmentIntelligenceDashboard/export_meters.py" --state-dir state --session <session-id> --currency USD --approval-db state/approval.db --output snapshot.json
```

Then open the ATLAS tab and choose **LOAD MEASURED SNAPSHOT**. The file is parsed in the page —
nothing is uploaded and no broker is contacted — and a reading without a source, a scope and a
plausible timestamp is rejected along with the whole file. Readings older than their
`stale_after_seconds` turn amber on their own, on a one-minute tick.

The HISTORICAL option is the first row of `reports/calibration_sweep.csv`: real out-of-sample
research, the first row rather than a selected winner, carrying no observation timestamp. The page
says so in the notice line instead of dressing it as current performance.
