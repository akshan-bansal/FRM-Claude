# Audit ledger + cryptographic continuity — scope

**Status:** drafted 2026-09-24. **Phases 0–7 built and tested** (see the phase table); phase 8
remains scope only and blocked on three decisions, five events remain unemitted (phase 5's row), and
the firmware half of phase 6 is specified but not written (phase 6's row). Against the code on `feat/multi-scoring-attention-map`.

**What this is:** an engineering structure for making the platform *auditable* — an event-sourced
ledger of every state transition, and an unbroken cryptographic chain from the displayed intent to
the device that approved it.

**What this is not:** legal or compliance advice, and not a claim of compliance with any regime.
This repo trades paper only, for one operator, so no books-and-records rule is presently binding.
The point of scoping it now is that audit trails cannot be reconstructed retroactively: if the
events aren't recorded as they happen, the evidence does not exist later. Where regimes are
mentioned below it is to name the *shape* of the requirement (immutability, time synchronisation,
order reconstruction, retention, change management), not to assert that any box is ticked.

---

## 1. What exists today

| Store | Written by | Contents | Correlatable? |
|---|---|---|---|
| `state/orders.jsonl` | `Router.submit` → `OrderJournal.order_intent` | mode, strategy, symbol, action, shares, entry, stop, target, risk_dollars, accepted, rejected_reasons | **no id** |
| `state/rejected.jsonl` | `Router`, `SessionRouter` | symbol + reasons | no id |
| `state/fills.jsonl` | `Router.submit` after `place_order` | mode, broker, order_id, symbol, shares, action | broker order id only |
| `state/paper_{orders,fills}.jsonl`, `paper_equity.csv` | `PaperBroker` | simulated orders/fills, equity curve | `session_id` |
| `state/approval.db` (`intents`, `cards`) | `ApprovalRouter` / store | intent_id, issued/expires/resolved, verdict, consumed, broker, symbol, action, shares, entry, stop, target, notional, risk_dollars, strategy, account, mode, thesis, intel_ref, nonce, **canonical**, signer_card_id | `intent_id` |
| `state/intel_graph.jsonl` | `PaperBroker` fill edges | `traded` edges: venue → symbol, notional, action/qty/price/session/order id | session + order id |
| `state/sizing_decisions.jsonl` | sizing v2 | per-decision sizing inputs/outputs | symbol + ts |
| `state/scheduled_intents.jsonl` | `SessionRouter` | queued / replaced / released / expired / rejected | symbol + action |

The approval path is already strong on WYSIWYS: `canonical_bytes()`
([approval.py:58](src/trading_live_claude/execution/approval.py:58)) binds broker, action, symbol,
shares, entry, notional, account, intent_id and nonce into the exact bytes the card signs, and the
router refuses any response whose signature does not verify.

### The seven gaps

1. **The signature is discarded.** `verify()` checks it, then nothing persists it — the `intents`
   table has `signer_card_id` but **no signature column**
   ([approval_sqlite.py:58](src/trading_live_claude/execution/approval_sqlite.py:58)). The single
   strongest piece of evidence ("this key signed these bytes") cannot be re-verified afterwards.
   Today's audit answer is "the router says it checked."
2. **No intent identity outside the card path.** `OrderIntent`
   ([router.py:52](src/trading_live_claude/execution/router.py:52)) has no id; `intent_id` is minted
   inside the approval layer. So `orders.jsonl` → `fills.jsonl` → `approval.db` cannot be joined,
   and a run without `--require-card` has no intent id at all.
3. **No versioning.** Nothing records which strategy *version* produced a signal or which gate list
   evaluated it. A backtest or a gate change silently invalidates the meaning of old rows.
4. **No payload hash.** `canonical` is stored as text in the DB only; no journal row carries a hash,
   so nothing detects a row edited in place.
5. **Sparse event coverage.** Three record kinds (intent+accepted flag, rejection, fill) against the
   eleven transitions below. `DECLINE`/`EXPIRED` live only in the DB; `PARTIAL` and `CANCELLED` are
   recorded nowhere; "displayed on the device" is never recorded at all.
6. **No tamper evidence.** Plain append-only JSONL, writable by any process, no chaining, no
   sequence numbers, no WORM. The test suite has twice written into these files
   (`NEXT_SESSION.md` queue), which is exactly the failure mode a ledger must make visible.
7. **No identity.** No operator/user id, and device identity exists only as `signer_card_id` on the
   card path.

---

## 2. The event ledger

One append-only ledger, one row per transition, as the **write** path; every existing journal
becomes a projection of it rather than a parallel truth.

### 2.1 Event taxonomy

The requested chain, plus the transitions this system actually produces (the extras are not
padding — each is a real branch that currently leaves no trace, or only a log line):

```
STRATEGY_SIGNAL ──────────── a strategy emitted entry/exit for a symbol
   ├─ SIGNAL_SUPPRESSED ──── overlay/interpret/persistence gate damped it before sizing
SIZED ────────────────────── sizer produced shares + stop (sizing v2 inputs/outputs)
RISK_CHECK ───────────────── Router._gate verdict, per-gate detail
   ├─ RISK_TRIMMED ───────── size-cap trim (symbol cap / leverage headroom)
   ├─ RISK_REJECTED ──────── with every reason
   └─ KILL_SWITCH_TRIPPED ── halt state entered/cleared
INTENT_CREATED ───────────── intent_id minted HERE; emitted by Router.announce_intent
   ├─ INTENT_QUEUED ──────── SessionRouter held it (venue closed); RELEASED / EXPIRED later
INTENT_SENT ──────────────── published to the approval shim
INTENT_DISPLAYED ─────────── device fetched/rendered it (currently unrecorded)
APPROVED / REJECTED / EXPIRED
SIGNED ───────────────────── signature bytes + verifying key id, PERSISTED
BROKER_SUBMITTED ─────────── order handed to broker (paper or live), with venue
FILLED / PARTIAL / CANCELLED / BROKER_REJECTED
POSITION_FLATTENED ───────── exit/flatten-on-exit, so a session's book closes on the record
FUTURES_ROLLED ───────────── contract change, so quantity/notional discontinuities explain themselves
CARD_REGISTERED / CARD_REVOKED
```

### 2.2 Record schema

Every event, same envelope. The requested fields map as shown; "new" means it does not exist today.

| Field | Source | Status |
|---|---|---|
| `seq` | monotonic per-ledger counter | new — gives ordering independent of clocks |
| `ts` | `datetime.now(UTC)` | exists |
| `event` | taxonomy above | new |
| `intent_id` | minted at `INTENT_CREATED` | **new at Router level** (gap 2) |
| `session_id` | `PaperBroker.session_id` | exists |
| `device_id` | `signer_card_id`, or `host:<machine>` for automated steps | partial |
| `user_id` | single operator constant now; a real field so multi-operator is additive | new |
| `payload` | event-specific body (see below) | — |
| `payload_hash` | `sha256(canonical_json(payload))` | new (gap 4) |
| `prev_hash` | `payload_hash` of the previous ledger row | new (gap 6) |
| `signature` | Ed25519 signature bytes, base64 | **new (gap 1)** |
| `signing_key_id` | card id / pubkey fingerprint that verifies it | partial |
| `strategy_id` + `strategy_version` | strategy name + content hash of its module and params | new (gap 3) |
| `risk_check_version` | hash of the ordered gate list + their thresholds | new (gap 3) |
| `broker_order_id` | `placed.id` | exists (fills only) |
| `execution` | fill price, quantity, fees, venue, residual | partial |
| `spec_version` | `SPEC_VERSION` already used for the URL contract | exists |

`payload` stays event-specific and is hashed as canonical JSON (sorted keys, no whitespace,
fixed float formatting — the same discipline `canonical_bytes` already applies, and for the same
reason: two writers must produce identical bytes or the hash is meaningless).

### 2.3 Integrity

Three layers, increasing in cost and strength. Each is useful alone; do not claim a stronger one
than is actually running.

1. **Hash chain.** *(Built, phase 3.)* Each row carries `prev_hash` — the previous row's `row_hash`,
   which covers the **entire envelope**, not just the payload, so a retimed or relabelled row fails
   too, and a forger who fixes one row's own hash still breaks the next row's link. Detects
   insertion, deletion, reordering and in-place edits of anything but the tail. Cheap. *Does not*
   stop an attacker who can rewrite the whole file, and is not WORM storage.
2. **Daily anchor.** At UTC rollover compute the Merkle root of the day's rows, sign it with a key
   that is not the trading process's, and append it (and optionally publish it off-box). Bounds
   rewrites to within the current day.
3. **Card counter-signature on decisions.** The device already signs intents; the same key can sign
   the approval event's `payload_hash`, which binds the ledger row to the hardware rather than only
   to the host.

Retention and WORM are a **policy choice, not code**: a copy to append-only external storage, plus
an explicit retention period, is what regimes normally want. Scope it separately; note that today
nothing prevents a stray process — including the test suite — from writing to `state/`.

### 2.4 Storage

- **Write path:** `state/ledger/YYYY-MM-DD.jsonl`, opened append-only, fsynced per row, never
  rewritten. Daily files keep a single file from growing without bound and make anchoring natural.
- **Read path:** a SQLite projection rebuilt from the ledger (`ledger_events`, plus materialised
  `intents` / `fills` views). Rebuildable from scratch — if the projection and the ledger disagree,
  **the ledger wins**, and disagreement is itself a reportable finding.
- **Existing stores stay** during migration, and a reconciliation check compares them against the
  ledger. They are retired only once it matches for a full session.

### 2.5 What the ledger must be able to answer

These are the acceptance tests, not prose:

1. Given an `intent_id`, produce the complete chain from signal to fill with timestamps, the
   signature, and the key that verifies it — from the ledger alone, with no live services.
2. Re-verify any historical approval offline: recompute canonical bytes from the recorded payload,
   check the stored signature against the recorded key.
3. Replay the ledger to reconstruct positions, cash and realised P&L, and match the journals.
4. Detect a tampered row (edit, insert, delete) via chain verification.
5. Explain every rejection: which gate, which threshold, which version of the gate list.
6. Show which strategy version produced a given trade, and what changed between versions.

### 2.6 Time: what our timestamps actually prove

Surveyed 2026-09-24, because "the ledger is timestamped" is the kind of claim that sounds like
evidence and currently is not. Every time value we hold comes from the machine writing the record.

| Value | Source | What it is worth |
|---|---|---|
| `ts` on every ledger row | `datetime.now(UTC)` | **untrusted** — the host sets it |
| `seq` + `prev_hash` | ledger envelope | strong for **order**; says nothing about wall-clock |
| `issued_at` / `expires_at` / `resolved_at` | `approval.db` | host clock |
| `intent_id`'s time prefix | **inside the signed canonical bytes** | see below |
| `esp_timer_get_time()` in the card's passbook | ESP32 | **uptime since boot**, not wall-clock |

**The one non-obvious property, worth not losing:** `mint_intent_id()` is
`f"{time.time_ns():016x}-{token_hex(6)}"`, and `intent_id` is one of the nine fields in
`canonical_bytes`. So the card's signature *does* cover a creation time —
`18d84e7c8730f1e0` decodes to `2026-09-24T16:25:48.630815+00:00`. An approval therefore cannot be
re-dated without invalidating its signature. But it is the *host's* claim about the time, merely
signed by the card: it proves the host asserted that time and the card approved bytes containing it,
not that the time was correct. Anyone changing the id format must keep this in mind, or silently
remove a property the record currently has.

**The card cannot help.** It has no clock by design: `prompt_ttl_seconds` derives the window from the
server's own `issued_at`/`expires_at` so no NTP is needed, and the countdown runs on FreeRTOS ticks.
The card can contradict the host about the *bytes*, never about the *time*.

**What is missing.** Nothing attests a time independently of the writer. Set the system clock back
and every new `ts` — and every new `intent_id` prefix — follows it, with `seq` and the chain still
verifying perfectly. There is also no monotonic-clock guard, so a backwards jump inside a session
leaves out-of-order `ts` values in a valid chain.

**Options, cheapest first** (decision deferred to phase 8):
1. Record `time.monotonic()` beside `ts`, so a clock jump *within* a session becomes visible. Cheap,
   weak, catches accidents only.
2. Publish the daily anchor root somewhere append-only and remote; the publication time is a
   timestamp we did not author, and it bounds every row beneath that root.
3. An **RFC 3161** timestamp token over the daily Merkle root from a timestamp authority: one HTTP
   call, a standard token, independently verifiable by anyone, no service of ours to run. This is the
   real answer if a timestamp ever has to convince a third party.

Note that the anchor in §2.3 does **not** solve this on its own: a root signed locally records a time
the same host chose. The anchor needs an external timestamp (2 or 3) before it means "this existed by
then" rather than "we say this existed by then".

---

## 3. Cryptographic continuity: dashboard ↔ card

Today the card displays and signs the canonical fields, but the dashboard (`pwa/index.html`) shows
**no fingerprint at all** — so a viewer cannot tell that the thing on screen is the thing the card
signed. The fix is small and worth doing early.

**Fingerprint** = first 8 bytes of `sha256(canonical_bytes)`, rendered as `7F3A...91C2`. Truncation
is for human comparison only; verification always uses the full signature over full canonical bytes.

```
INTENT                  DEVICE
AAPL BUY 100            VC-000173
LIMIT 245.50
                        STATUS
HASH                    AWAITING APPROVAL
7F3A...91C2
```

Then the same fingerprint appears in three places — dashboard, device screen, ledger row — and the
signature proves *this intent → this device → this approval*.

**Where it touches** (all done 2026-09-24 except the last):
- ✅ `fingerprint()` beside `canonical_bytes()` — additive, no wire-format change, so deployed card
  signatures stay valid.
- ✅ `/v1/intents` and `/v1/passbook` carry `fingerprint`; the OpenAPI description states plainly
  that it is a human comparison value and that verification uses the full signature.
- ✅ `pwa/index.html` renders it on each pending prompt (HASH / ACCOUNT / STATUS with a live TTL)
  and on each history row with the signing card id.
- ✅ `approval_card_sim.py` computes it locally (its own SHA-256, as firmware must) and refuses to
  sign on mismatch.
- 🔴 **Firmware — specified, not written.** `firmware/tradecard/main/main.c`, in `handle_prompt`
  after `parse_canonical` succeeds: hash the same `canon_buf` bytes with ESP-IDF's bundled mbedTLS
  (`#include "mbedtls/sha256.h"`, `mbedtls_sha256((const unsigned char *)canon, strlen(canon), out,
  0)`), format `out[0..1]` and `out[30..31]` as `%02X%02X...%02X%02X`, and draw it on the LCD. The
  display is 4 lines and already full, so **which line it replaces is a UX decision** — likely the
  thesis line, since the hash is the security-relevant field. If the prompt also carries a
  `fingerprint` field, compare and take the REFUSED path on mismatch, exactly as the sim now does.
  Needs a device build to verify; not attempted blind.

**What it proves, and what it doesn't.** It proves the approval is bound to those exact bytes and
that key. It does **not** by itself prevent a host that lies to both screens: the dashboard must
derive the fingerprint from the *same* record the card fetched, never recompute it from separate
inputs, or the display and the signature can drift apart. Replay is already covered by the nonce
and `consumed` flag; keep both in the ledger record.

---

## 4. Phases

Each phase is independently shippable and leaves the system working.

| # | Phase | Why this order | Rough size |
|---|---|---|---|
| 0 | ✅ **DONE 2026-09-24.** Partial sells now realise `(fill − avg) × closed_qty`; zero-crossing handled; `resume` accepts a pre-fix journal (legacy accumulator) rather than stranding a book, and `state/` is never rewritten. Replaying real session `fba831e3` reproduces the measured **−$39.92** exactly. 5 tests. | A ledger built over wrong numbers launders them into "audited" numbers | done |
| 1 | ✅ **DONE 2026-09-24.** `intents` gained `signature` + `signature_alg` with an additive migration (verified on a copy of the live 19-row `approval.db`); `audit_record()` returns canonical + signature + the signer's pubkey (even if since revoked); `verify_audit_record()` re-verifies offline and **never reports missing evidence as a pass**; `fingerprint()` added. 6 tests. Old rows stay NULL — that evidence cannot be backfilled. | Closes the worst gap; unblocks offline re-verification | done |
| 2 | ✅ **DONE 2026-09-24.** `OrderIntent.intent_id` minted at construction, journalled on accept / gate rejection / fill / broker rejection / queue events, and reused (not re-minted) by both approval stores, so it is inside the signed canonical bytes. 7 tests. | Every later phase needs one correlation key | done |
| 3 | ✅ **DONE 2026-09-24.** `audit/ledger.py`: append-only `state/ledger/<date>.<stream>.jsonl`, fsync per row, full envelope, `payload_hash` + `prev_hash` chain **over the whole envelope** (stronger than the payload-only chain first sketched below), resume across restarts and day rollover, torn-tail tolerance, non-strict by default with `strict=True` available. Router dual-writes RISK_CHECK / RISK_REJECTED / BROKER_SUBMITTED / FILLED / BROKER_REJECTED when passed a ledger, and is unchanged without one. `scripts/verify_ledger.py` verifies every chain (exit 1 on a break) and reconstructs one intent's history. Wired into `paper_kraken.py` behind `--audit-ledger` (on). 20 tests, most of them attacks: edited payload, edited envelope field, deleted row, reordered rows, spliced row, self-consistent forgery. | Evidence starts accruing; nothing is retired yet | done |
| 4 | ✅ **DONE 2026-09-24.** `audit/versioning.py`: `strategy_version` = hash of the strategy module's source + that instance's params (init kwargs *and* the per-trade exit knobs, so `bollinger(n_std=2.75)` versions differently from the default); `risk_check_version` = hash of `Router._gate`'s source + every threshold read off the live router, so adding or reordering a gate moves it even with thresholds untouched. Both land on every ledger row. `OrderIntent.strategy_version` pins a version for a replayed intent; otherwise the Router asks `strategy_version_for`, which `LiveMonitor` installs from its own instances (one wiring point, not every intent site). Unversionable code reports `"unknown"`, never a hash. `verify_ledger.py --intent` prints both. 16 tests, including one asserting all nine thresholds move the version. **Documented limit:** strategy versions hash the module's own source and do not follow imports, so an edit to a shared helper (`signals/indicators.py`) changes behaviour without changing the version. | Makes old rows interpretable | done |
| 5 | ✅ **MOSTLY DONE 2026-09-24.** Emitting now: `STRATEGY_SIGNAL` / `SIGNAL_SUPPRESSED` (with the reason: overlay halt, persistence halt, or sized to zero — this removes the phase 3 ambiguity where an empty ledger looked identical to a broken one), `RISK_TRIMMED` (original size, trimmed size, and which cap bound), `PARTIAL` vs `FILLED`, `BROKER_REJECTED`, `INTENT_QUEUED` / `INTENT_RELEASED` (with the release reprice), `INTENT_SENT` (with the fingerprint), `APPROVED` / `REJECTED` / `EXPIRED`, `SIGNED` **carrying the signature and signing key**, and `POSITION_FLATTENED` (recording an incomplete flatten, the case an auditor most wants to find). Two real gaps surfaced while testing: the approval path's *accepted* pre-prompt gate emitted nothing (chain began at `INTENT_SENT`), and the Router compared fill quantity against an order object the broker had already mutated. Both fixed. 12 tests, including the scope's acceptance criterion — a full `RISK_CHECK → INTENT_SENT → APPROVED → SIGNED → RISK_CHECK → BROKER_SUBMITTED → FILLED` chain reconstructed from the ledger alone, with the signature re-verified offline from the ledger row. **Still unemitted:** `INTENT_DISPLAYED` (needs the store to record when the card first fetched a prompt), `SIZED` (folded into the signal payload for now), `KILL_SWITCH_TRIPPED`, `CARD_REGISTERED` / `CARD_REVOKED` (the registry has no ledger handle), `FUTURES_ROLLED` (IB path isn't trading), `CANCELLED` (nothing cancels orders today). | Completes the chain | mostly done |
| 6 | ✅ **DONE 2026-09-24 except firmware.** `Prompt.fingerprint` and `PassbookEntry.fingerprint` are derived from the stored canonical bytes (never recomputed from the JSON fields), on the wire in `PromptOut` / `PassbookEntryOut`, rendered by `pwa/index.html` (HASH / ACCOUNT / STATUS on a pending prompt; fingerprint + signing card in history — it displayed none before), and **recomputed independently by `approval_card_sim.py`, which now REFUSES to sign when the served fingerprint disagrees with the bytes in front of it.** 10 tests, including prompt/passbook/ledger agreeing on one value for one intent, and a venue swap or resize changing it. **Firmware NOT written** (see below) — I cannot build or flash ESP-IDF here, and shipping unverified C into the approval path risks breaking the refuse path. | Visible continuity; small but crosses repos | done (firmware specified) |
| 7 | ✅ **DONE 2026-09-24.** `audit/projection.py` replays `state/ledger/*.jsonl` into a derived, disposable SQLite read model — `ledger_events` (flat spine, keyed `(stream, seq)`), `intents` (one row per intent, folded from its events), `chain_status` (last verification per stream, so "is the record intact" is queryable). Rebuilt from scratch each time rather than migrated: a projection that can only move forward eventually disagrees with the ledger in ways nobody can explain. Query helpers answer the §2.5 questions: gate-rejection counts, `INTENT_SENT`→verdict latency, which strategy version traded a symbol, intents that reached the broker with no terminal event (the crashed-writer case), and every signed intent with what is needed to re-verify it. `scripts/rebuild_projection.py` prints all of it and exits 1 on a broken chain; `scripts/reconcile_ledger.py` compares ledger rows against `fills.jsonl`, `rejected.jsonl` and `approval.db`, failing only on journal rows the ledger lacks (ledger-only rows are progress, not a discrepancy) and classing pre-intent-id rows as unjoinable rather than mismatched. 20 tests. Verified on a seeded ledger: 11 events → 3 intents, outcomes folded, 2 journal fills and 1 rejection reconciled exactly. | Query surface; proves the ledger is complete | done |
| 8 | **Daily Merkle anchor + signed root**; then retention/WORM policy | Strongest integrity claim; policy decisions needed | medium, needs your call |

**Dependencies:** 1 and 2 are independent and both precede 3. 4–6 follow 3. 7 needs 3+5. 8 last.

## 5. Risks and open questions

- **Write amplification on the hot path.** fsync per event inside a 300s poll loop is fine; inside a
  tick-level loop it is not. Ledger writes must stay on the decision path, never per quote.
  (`Ledger(fsync=False)` exists for tests and any future high-rate stream.)
- **An empty ledger is currently ambiguous** (observed 2026-09-24): a two-poll Kraken session with
  24 HOLDs and no entries wrote zero rows, which is *correct* — phase 3 records the order path only —
  but indistinguishable from a ledger that is silently broken. Phase 5's `STRATEGY_SIGNAL` /
  `SIGNAL_SUPPRESSED` events remove the ambiguity; until then, absence of rows is not evidence of
  absence of activity.
- **Clock trust — surveyed, see §2.6.** Every timestamp we hold is the writing host's own claim;
  `seq` carries ordering regardless. The signature covers a creation time only via the `intent_id`
  prefix, which is the host's claim signed by the card. A trusted timestamp (RFC 3161, or remote
  publication of the daily root) is a phase 8 decision, not something the chain provides.
- **Key management.** Ledger anchoring needs a key the trading process cannot use, or the anchor
  proves nothing about the process that wrote the rows. Where does it live?
- **Paper vs live.** Every record must carry `mode`. An audit trail that cannot distinguish
  simulated fills from real ones is worse than none.
- **`user_id` today is a constant.** Recording it now is cheap; inventing multi-operator identity
  now is not.
- **Scope creep into compliance.** Retention periods, supervisory review and reporting are policy,
  not code. Decide them explicitly before building anything that claims to satisfy them.
