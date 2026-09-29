# Graph influence channel + `scaled` edge — scope

**Status:** drafted 2026-09-29. Specification only; nothing below is built. Against the code on
`feat/multi-scoring-attention-map`.

**What this is:** a design for making the intel graph able to answer *"which intelligence resized
this order, and by how much"* from the record alone.

**What this is not:** a model that emits trades. Nothing here lets intelligence increase a position,
and nothing here closes a feedback loop from P&L back onto weights. Both are deliberately excluded;
see §6.

---

## 1. The problem, measured

The graph has **two disconnected components** (verified 2026-09-29 over 14,265 edges / 635 nodes):

| component | node types |
|---|---|
| execution | `analysis`, `symbol`, `venue` |
| intel | `domain`, `event`, `market`, `poll`, `region`, `source` |

`symbol` is touched by exactly two predicates — `analysis ranked_by symbol` and
`venue traded symbol`. **No edge connects an event, domain or region to a symbol or a trade.** So a
fill cannot be traced back to the intelligence that shaped it, at any weighting. This is a topology
gap, not a tuning gap: reweighting cannot create an edge that does not exist.

Meanwhile the influence **already happens and is already computed**. `monitor/live_loop.py:657`
builds a sizing row containing `mitigation.scalar` — the overlay's de-risk multiplier — and that
scalar materially changes the order size. Observed live on 2026-09-29: LINK/USD sized at
`mitigation 0.474`, i.e. the intel layer removed 52.6% of the position. The number lands in
`state/sizing_decisions.jsonl` and never becomes an edge.

## 2. `weight` today is three incompatible things

Measured across the journal:

| predicate | n | min | max | negative |
|---|---:|---:|---:|---:|
| `observed` | 9287 | 0.000 | 129.8 | 0 |
| `about_domain` | 4428 | 0.017 | 1.0 | 0 |
| `stressed_by` | 336 | 0.010 | 69.0 | 0 |
| `traded` | 241 | −74,957.163 | 100,046.0 | 81 |
| `ranked_by` | 45 | −61.242 | 17.6 | 23 |
| `elevated_in` | 3 | 0.020 | 0.0 | 0 |

Three semantics share one field: non-negative evidence strength, **signed notional dollars**
(`traded`), and a **signed rank score** (`ranked_by`). There is no single channel an influence model
could read. Any propagation over `weight` is dominated by `traded`, which is 3–4 orders of magnitude
larger than every intel edge and is not an influence at all.

## 3. The `influence` channel

Add one field to `Edge` (`intel/graph.py`):

```python
influence: float | None = None    # dimensionless multiplier in (0, 1]; None = carries no influence
```

Rules, all enforced at construction:

1. **Dimensionless.** Never dollars, never a score. A pure multiplier.
2. **Clamped to `(0.0, 1.0]`.** This preserves the invariant that
   `tests/test_intel_overlay.py::test_overlay_only_ever_reduces` already asserts of the overlay
   (`0.0 < scalar <= 1.0`): intelligence may de-risk and may never lever up. The clamp lives on the
   channel, so the property survives even if a future caller passes something larger.
3. **`None` means neutral, not zero.** A predicate that carries no view leaves it unset. Consumers
   treat `None` as "not part of the influence path", never as a multiplier of 0.
4. **`weight` is untouched.** It stays the native record in its own per-predicate unit. `influence`
   is a second, separate channel. Nothing sums or compares the two, and nothing pools `weight`
   across predicates.
5. **Serialised only when set**, so existing journal rows stay byte-identical and `from_row`
   defaults it to `None`.

### Neutral venue (objective #1)

`traded` sets `influence=None`, permanently. A venue is where a trade cleared, not why it happened;
Kraken versus Questrade says nothing about whether an order was justified. Its signed notional stays
in `weight` and its detail in `meta`, both still queryable — the influence path simply does not read
it. This is the whole of objective #1: no journal rewrite, no lost data, the model just stops
listening to the one edge that would otherwise drown every other.

`ranked_by` keeps its **sign** in `weight`. Half its edges are negative (23 of 45) and those
negatives are real expertise — "this name ranks badly" is as informative as the converse. It is a
candidate for a later, separately-specified influence mapping; it does not get one here, because
mapping a signed rank onto a (0,1] multiplier is a modelling decision, not a plumbing one.

## 4. The `scaled` edge

```
(poll, <as_of>)  --scaled-->  (symbol, <SYM>)
```

**Why `poll` is the subject.** The scalar is produced by one overlay read of one snapshot. That read
is already a node (`poll`, 179 instances) and is already connected to `event`, `region` and `domain`.
Hanging the edge off the poll joins the two components *through the thing that actually produced the
number*, and makes the provenance chain traversable: symbol → poll → observed events → sources. The
alternative — inventing an asset-class node — would add a type that nothing else references and
would still need a poll to be honest about which read it came from.

Constructor mirrors `fill_edge()` (`intel/graph.py:505`), which is the established pattern for an
edge emitted from the trading path:

```python
def scaled_edge(*, poll_id: str, symbol: str, scalar: float, asset_class: str,
                intent_id: str, strategy: str, reasons: list[str], as_of: str) -> Edge:
    """One `scaled` edge: an overlay read resized an intent for this symbol.

    weight    = the scalar, for a human reading the journal
    influence = the same number as the model's channel, clamped
    meta      = intent_id (the join key), asset_class, strategy, reasons
    """
```

- `predicate`: `"scaled"`, added to the `Predicate` Literal.
- `weight`: the applied scalar.
- `influence`: the same value, clamped to `(0, 1]`.
- `meta.intent_id`: **the join key.** This is what makes a fill joinable to the intelligence that
  resized it, and it is the field without which the whole exercise is decorative.
- `meta.asset_class`, `meta.strategy`, `meta.reasons`: the overlay's own stated reasons, carried
  verbatim so the panel can show *why* without re-deriving it.

### Emit site

`monitor/live_loop.py`, at the point the sizing row is built (~line 657), where `mitigation.scalar`
is already in hand. Wrapped in its own `try/except` like every other journal write in the trading
path — **a graph write must never break a trading step.**

### Decay policy

`DEFAULT_POLICIES` gets no entry for `scaled`, deliberately. `wash_edges` leaves a predicate with no
policy unchanged, which is correct here: a `scaled` edge is a **statement about a decision that was
made at a moment**, not evidence whose relevance fades. Decaying it would corrupt the audit trail it
exists to provide. (`traded` and `ranked_by` are already policy-free for the same reason.)

## 5. Coverage, stated up front

`journal_row` is only built when `policy is not None` — i.e. **sizing v2, which is Kraken-only by
standing rule**. So on day one the `scaled` edge covers the crypto sleeve and not the 117-fill
Questrade path. That is a real limitation and must be visible wherever the derived attribution is
displayed, as a stated scope rather than a silent gap: *"attribution covers N of M fills"*.

Closing it means the QT path emitting its overlay scalar too. That is a separate change and does
**not** mean porting sizing v2 to QT.

## 6. Explicitly out of scope

- **Feedback from outcomes onto weights.** Deferred on evidence, not on caution. The base is 7
  closed round trips at a 28% gross / 10% net win rate against a flat $4.95 per fill — at this
  ticket size the outcome signal is dominated by the fee schedule, so a loop fitted on net P&L
  would learn the broker's pricing rather than anything about intelligence. It is also
  self-reinforcing: a scalar that precedes a winner gets upweighted, sizes the next one larger, and
  the following outcome becomes a function of the sizing rather than the signal. Revisit when there
  are enough closed round trips at a size where edge exceeds fees — which argues for larger tickets
  before the question is answerable at all.
- **Intelligence increasing a position.** The `(0, 1]` clamp forbids it by construction.
- **`ranked_by` → influence mapping.** Needs a modelling decision about signed scores; not plumbing.

## 7. Acceptance

1. `Edge.influence` round-trips through `to_row` / `from_row`; absent field reads back `None`;
   existing journal rows parse unchanged.
2. Construction clamps: `influence=1.4` stores `1.0`; `influence=0.0` or negative is rejected.
3. `traded` edges always carry `influence=None`.
4. A `scaled` edge is emitted per sized intent, carrying `meta.intent_id`.
5. The type graph becomes **one connected component**, verified the same way §1 was measured.
6. A fill's `intent_id` resolves through `scaled` to a poll, and from that poll to its observed
   events and sources — the reverse-derivation this whole document exists for.
7. `wash_edges` leaves `scaled` edges unchanged.
8. A graph-write failure at the emit site does not interrupt the trading step.
