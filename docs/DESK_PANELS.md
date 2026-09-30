# TradeDesk panels — the denormalization gradient

Design spec, 2026-09-29. **Not built** beyond panel #1's feed (`GET /v1/books`). Written down first
because the ordering is the design: the panels are not a menu, they are a **gradient from
denormalized to normalized**, and each step earns the right to join data the previous step kept apart.

## The one rule the whole thing rests on

Every figure is read from this repo's own execution path — journals (`state/paper_*.jsonl`,
`paper_equity.csv`), the audit ledger, the approval store, the intel graph — and carries the file it
came from, the scope it covers and when it was observed. A figure that cannot be sourced that way is
**not shown with a caveat; it is not shown.** A book whose journal has no rows is reported UNREAD,
never as zero. No specimen rows, no demo data, no constants.

Corollary for this dashboard specifically: **normalization is a claim.** Summing a CAD equity book
and a USD crypto book into one "NAV" invents a number no journal contains. So the gradient only
normalizes where a shared key genuinely exists.

## One port, three brokers (built 2026-09-29)

All three loops write prompts and card pubkeys to the same `state/approval.db`, so the store was
already shared; what collided was the HTTP surface — every loop booted its own shim on 8787 with its
own token. Now:

```bash
# one shim, owning the port, the token and the desk panel, declaring all three books
python scripts/approval_shim.py --db state/approval.db --state-dir state --session-id <primary> \
  --book questrade:<qt-session>:CAD:equity \
  --book kraken:<kr-session>:USD:crypto \
  --book ib:<ib-session>:USD:derivatives

# each loop attaches instead of binding a port
python -m trading_live_claude.cli signal ... --paper --require-card --card-attach
python scripts/paper_kraken.py ... --require-card --card-attach
python scripts/paper_ib.py     ... --require-card --card-attach
```

One card, one token, one panel, three books. A loop run without `--card-attach` still boots its own
shim and declares its own single book, which is the old behaviour.

## Panel 1 — DESKS (fully denormalized, by brokerage x asset class)

**Source:** `GET /v1/books` — one row per `BookRef(venue, session_id, currency, asset_class)`, each
carrying its own `audit.meters` snapshot. **Deliberately un-summed**, and the response says so.

| column | from |
|---|---|
| venue / asset class | the book declaration |
| NAV, cash, exposure %, P&L | `paper_equity.csv` for that session |
| session id, mode (LIVE / UNREAD / UNREADABLE) | the journal's state |

Why first: it is the only view that needs no reconciliation. Three books in three currencies on three
venues sit side by side and nothing is claimed about their relationship. A reader who wants the total
has to ask for it, and the next panels are where that question gets earned.

## Panel 2 — BOOK (normalizes within a venue)

Positions, fills and realized/unrealized P&L per book, from `paper_fills.jsonl` +
`paper_orders.jsonl`. The shared key is the session id, so aggregation inside one book is sound:
same currency, same venue, same cost model. First place a total appears — and it is a
**per-book** total.

Carries the known accounting caveats where they apply: partial sells booked correctly only after the
2026-09-24 fix, and a session killed with positions open is excluded from realized-P&L aggregates
(48 of 79 historical sessions are in that state) rather than shown at $0.00.

## Panel 3 — CHAIN INTEGRITY (normalizes across the decision path)

The join is one intent's life: signal -> sizing decision -> gate verdict -> card prompt -> signature
-> fill -> journal row. Sources: `sizing_decisions.jsonl`, `paper_orders.jsonl` (with
`rejected_reasons`), the approval store (verdict, signer, canonical bytes, fingerprint), the audit
ledger, `paper_fills.jsonl`.

What it must show, because this is where silent failure lives: intents with **no** downstream row,
fills with no prompt, prompts that expired unapproved, gate rejections by reason, and any
fingerprint mismatch between what the card signed and what was sent. This panel's value is the
**gaps**, so an empty cell is information and must render as such, never as a dash that reads like
zero.

## Panel 4 — ATLAS (normalizes to one wire schema)

`audit.meters` readings in the schema `export_meters.py` writes, so a live snapshot and an uploaded
one are treated identically — including the rule that each reading carries source, scope and
observed-at. This is where cross-book comparison becomes legitimate, because the schema forces each
number to declare its scope: a reading scoped "entire approval database" sits visibly apart from one
scoped "paper session X".

Currency still does not unify here. Two NAVs in different currencies are two readings, not a sum,
until an FX rate with its own source and timestamp is a first-class reading too.

## Panel 5 — GRAPH INTEL (fully normalized: the graph is the join)

`state/intel_graph.jsonl` — subject / predicate / object edges with weights and `as_of`, plus the
overlay and thesis layer (`intel/interpret.py`). Everything upstream becomes nodes: venues, symbols,
sessions, fills, events, domains. The graph is the most normalized view precisely because it stores
relationships instead of assuming them.

Two honesty requirements carried from the thesis work: theses are **research context, not signals**,
and the layer has never been scored against outcomes — so a panel showing a firing thesis shows its
gate readings and base rate beside it, or it overstates what a firing means. Four of the eight
theses are gated on values the feed has never reached; that belongs on screen too.

## Ordering rationale, in one line each

1. **DESKS** — no join, no claim. Safe with three heterogeneous books.
2. **BOOK** — join on session: same currency, same venue, same costs.
3. **CHAIN** — join on intent id: shows where the path breaks.
4. **ATLAS** — join on a schema that makes every scope explicit.
5. **GRAPH** — join on stored relationships rather than assumed ones.

Each panel may aggregate only on the key it actually shares. The gradient is the safeguard: by the
time a number is normalized, the panel behind it shows what it was normalized from.

## Open before building past panel 1

- `/v1/stats` and `/v1/meters/snapshot` still take a **single** `session_id`; `/v1/books` is the
  multi-book route. Either the panel reads per-book routes, or those two grow a book parameter.
- The desk page (`pwa/desk.html`, built by `scripts/build_desk_dash.py`) is read-only by
  construction and must stay so: the ACCEPT that decides an intent is a signature made on the card.
- FX: no rate is journalled anywhere today, so any cross-currency total is unbuildable by the rule
  above. Decide whether an FX reading becomes a first-class meter before promising a desk-wide NAV.
