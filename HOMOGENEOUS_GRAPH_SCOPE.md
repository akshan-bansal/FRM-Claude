# Homogeneous graph journal + society-weighted execution chain — scope

**Status:** drafted 2026-10-03, revised the same day to fold in factors, the spectral-filter files and
transaction costs. **Specification only; nothing below is built.** Extends `GRAPH_INFLUENCE_SCOPE.md`
(the `influence` channel and the `scaled` edge, also unbuilt) and uses the OASIS runner
(`vm/oasis_runner.py`), `bsm_spectral.py`, `bsm_parallel_surface.py`,
`financial_spectral_filters.py` and `sp100_mp_portfolio_risk.py`.

**Base — found 2026-10-03.** The working tree is `C:\Users\PC\Downloads\FRM-Claude` (cloned from
`github.com/akshan-bansal/FRM-Claude`, still named by the `frm-daily-push` scheduled task). **That folder
no longer exists on this machine.** The newest copy of the code is the GitHub branch
**`feat/multi-scoring-attention-map-wq7f42`, commit `97067ec`, 2026-09-30**: 579 files, including
`analysis/rmt.py`, `execution/venue_costs.py`, `audit/ledger.py` + `projection.py`,
`risk/entry_allocation.py`, `signals/oversold_entry.py`, `execution/basket.py`, `sim/contract.py`,
`scripts/oasis_sim.py`, `vm/oasis_runner.py`. This document should be built against **that**, not
against `Downloads\FRM-Claude-tradecard` (older: none of those files) or the 2026-09-17 tip of
`feat/multi-scoring-attention-map` (also older).
**Not on GitHub, so possibly lost:** everything after 09-30 (the 10-01 work: `audit/session_fitness.py`,
`kraken_equi_fetch.py`, `adapters_check.py`, the card spec 1.2.0 changes), all of `state/` (the journals),
`.env`, `config/trading.yaml`, and the **newer `vm/oasis_runner.py`** — the branch's copy still has `drive`
unwritten; the version with `drive` and the workspace header exists only in the local edit history.
No code has been changed. Re-check file names against the branch before building.

**What this is:** one typed graph journal that carries the whole chain — observation → society stance →
factors and eigen-structure → overlay → curvature energy → cost → order → fill — so that "which
intelligence, and which simulated society's view, resized this order, and by how much" is answerable
from the record alone.

**What this is not:** a model that emits trades, a way for intelligence to add exposure, or a loop that
fits weights to P&L. All three stay excluded (§6).

---

## 0. What is built (2026-10-03), in `E:\FRM-Claude-main` (a clone of the `-wq7f42` branch)

Uncommitted and unpushed (standing rule: commit only on request). Test status is in the Router section below.

- `intel/graph.py`: node types `society`, `factor`; predicates `holds_stance`, `loads_on`; decay
  policies for both. `Edge.influence`, `clamp_influence`, `scaled_edge` already existed on the branch.
  **Bug fixed:** `wash_edges` compared a signed weight with `min_weight`, so it deleted every
  negative-weight edge on the first wash. Harmless for the old predicates (all non-negative), fatal for
  signed stances. Now compares magnitude; regression test added.
- `sim/society.py` (new): `AdapterParams` (no defaults), `society_influence` (L3), `compose`,
  `top_eigenvalue_share` (refuses fewer than 30 agents), `society_edges`, `society_scaled_edge`,
  `cost_check_after_scaling` (L5).
- `tests/test_society_graph.py` (new): adapter invariants including `influence <= 1` over a dense grid,
  composition, the connected-component checks, decay, journal round trip, cost after scaling.

**Router wiring (2026-10-03).** `OrderIntent` gained `society_influence`, `society_run_id` and
`society_applied`; `Router` gained `society_view` (a `symbol -> (influence, run_id) | None` callable,
**off unless set** and set by nothing in any launcher yet). `Router._apply_society_influence` runs as
gate step 0 inside `_gate`:
- entries only, never exits; the multiplier is capped at 1, so it can only reduce an order; NaN, zero,
  negative, boolean or non-numeric means "no view" and the order goes through unchanged;
- a raising resolver is "no view", never a failed trade;
- it runs BEFORE min-ticket, the size caps and the cost gate, so all three judge the scaled size;
- scaled to zero shares is rejected with the reason, never rounded up;
- idempotent via `society_applied` (the card path gates, waits, then submits the same object);
- journaled in `orders.jsonl` only when it scaled something (other rows stay byte-identical), and
  written to the ledger as `RISK_TRIMMED` with the run id.
`SocietyView` (in `sim/society.py`) is what you assign: built from a validated result, it goes silent
when older than `max_age_h` (required, no default) or dated in the future.
**Found while wiring, fixed:** `SessionRouter._route` gates a COPY of the intent (the lot probe), then
submits the original. The scaling record lived only on the copy, so the real gate pass scaled a second
time (0.55 acted as ~0.30), and blindly copying the flag back would have let an order through at full
size when society had trimmed the probe to zero. `scheduler.py` now carries the record over only when
the probe really scaled. Both breakages were reintroduced on purpose and the new tests caught each.
**Side effects to know:** `risk_check_version` (the hash of the gate source) changes, so ledger rows
before and after this change carry different versions, which is the intended audit signal; `risk_dollars`
is not re-scaled when shares shrink (same as the existing size-cap trims), so the heat gate stays
slightly conservative.
**Verified:** 54 new tests in `test_society_graph.py`, `test_router_society.py`,
`test_scheduler_society.py`; the whole suite on the final code (2026-10-04, after the adapter, journal writer and tail
module below were added): 1725 passed, 2 skipped, 0 failed in 7m42s, `test_quantconnect.py` excluded as
the repo does. That count includes another session's new tests in the same checkout
(`lens_audit`, `curvature`), which I did not write. An earlier "1440" figure was an undercount from
broken progress lines, and one all-at-once run stalled at about 89% while several of my jobs shared the
machine; neither recurred. `mypy --strict` has no errors in `router.py`,
`scheduler.py`, `sim/society.py` (the 17 in `intel/graph.py` are in lines I did not touch); ruff is
clean on every new file.

**Deliberately not built:** the `agent` node type (the result contract only carries per-symbol
aggregates, so there is nothing per-agent to store) and `curved_by` (L4 is unconnected to orders).
The Router seam exists, but nothing sets `society_view`: no launcher, no `LiveMonitor` hook, no CLI
flag. The `herd` term needs a per-agent stance matrix the current result file does not carry. No
named-factor data, no `loads_on` writer yet, nothing run against a real LLM.

**Graph adapter, journal writer and edge-tail ruler (2026-10-04).**
- `sim/oasis_graph.py`: reads an OASIS run's database read-only and emits typed edges: `member_of` (every
  agent, so a silent agent is still on the record), `follows`, `engaged` (counts per ordered pair, with the
  like / dislike / comment / repost split), `attends` (how many items an agent wrote mentioning each
  symbol). Counts and ids only: post and comment text is untrusted model output and never reaches an
  edge (a test feeds it an injection attempt). The runner's own seed post is skipped so agent 0 is not
  credited with attention to every watched symbol. `herd_from_run` gives the top-eigenvalue share of the
  agents' symbol-attention matrix, so the `herd` term of the adapter no longer needs a per-agent stance
  matrix; it still refuses fewer than 30 agents.
- `sim/journal.py`: `journal_run` writes a run into `state/intel_graph.jsonl` exactly once: a `seeded`
  edge poll -> society (so "which intelligence did this society read" is a walk), the reported stances,
  and the database structure. It verifies the write landed, because `append_edges` swallows errors by
  design. `scripts/oasis_sim.py ingest <result> --db <oasis_RUNID.db>` does it in the normal flow.
- New node type `agent`; new edge types `member_of`, `follows`, `engaged`, `attends`, `seeded`, each with
  the same 24-hour half-life and 7-day lifetime as `holds_stance`.
- `analysis/edge_tails.py`: skew and generalised-Pareto peaks-over-threshold fits for any edge family's
  `weight` or `influence`, lower and upper tail, with a bootstrap interval for the shape, a
  parametric-bootstrap goodness-of-fit p-value, return levels, and a threshold-stability table. It
  refuses (too few exceedances; repeated values, which are mass at a clamp or integer counts, not a
  tail). Seeds are required. Measured on synthetic data: over 120 true tails it rejected 5.0% at the 5%
  level and 10.0% at 10% (calibrated); shape estimate mean 0.248, spread 0.06, against a true 0.25; a
  two-spike tail was caught at p = 0.005. It is a ruler: nothing in execution, risk, brokers, strategies
  or the daemon may import it (a test enforces both directions), and it feeds nothing.
- **Found by the tests:** the first symbol matcher matched `ON` inside "carry on". It now needs a whole
  token, and tickers of three letters or fewer must be upper-case.
- **Run on the real 10-03 database:** 5 agents, the seed post skipped, zero items written by any agent, so
  the only edges are 5 `member_of`. A society that did nothing looks like that in the graph.
- Limitation, stated: `attends` counts mentions and says nothing about direction; a stance still comes
  from the model readout. There is no real run to apply `edge_tails` to yet (no journal on this machine),
  so no statement about the shape of any real edge family has been made.

**Membership model and owner-only allocation (2026-10-04).**
- `sim/membership.py`: a synthetic scenario generator for cardholders joining and leaving, as asked:
  transactions proportionate to membership, a mean-reverting activity gap around exact cyclical
  harmonics (the cycle sits on top of the reverting term; my first version made the level chase a moving
  cyclical target, which low-passed a weekly swing to a quarter of its stated size, and the estimator
  caught it), attrition that rises when activity is below its cycle, and rare mass-exit shocks with a
  capped generalised-Pareto size, which is what makes membership change left-skewed. Every panel says
  SYNTHETIC. `estimate` recovers the settings from a panel alone (round trip: transactions per member,
  reversion speed within 0.06, innovation sd within 0.03, weekly amplitude within 0.04, median attrition),
  and refuses small or sparse panels. `panel_from_approval_db` maps real records into the same panel: a
  card is a member, a revocation is attrition, a signed approval (`ACCEPT`) is an active transaction.
  Lapse attrition (no approval for N days) is inferred, so its definition is a required argument and the
  panel's notes say it was used. **The real store on this machine has 0 cards and 0 approvals, so the
  map returns an empty panel that says so; nothing real has been fitted.**
- `return_allocation.py` and `scripts/allocate.py`: an owner-only way to split a return among members.
  Money is integer cents; `pro_rata` uses largest-remainder rounding (verified to the cent over 3,000
  random splits), `explicit` must sum to the total. A plan is recorded only if the exact bytes of the
  request carry a valid Ed25519 signature from an owner key (keys separate from the trade-approval
  cards; a trade canonical cannot authorize an allocation because the allocation bytes begin `alloc/1`).
  Requests expire soon, authorize once, and every attempt including refusals goes in a hash-chained
  journal that `verify` re-checks offline: an edited row, a deleted row, or a row signed by anyone but the
  owner is reported even if the forger rebuilt every hash. Signing needs your private key and an
  interactive `YES`. **It moves no money.** Honest limit: the gate runs on the machine it protects, so
  someone who can edit the code can call around it; they cannot forge a signature that verifies against
  your public key, so the bypass is visible in `verify`. The real card firmware signs only trade
  formats, so allocations are signed with a software owner key until firmware supports an allocation
  screen. To keep an automated assistant out as well, the project's deny list can carry
  `Bash(python scripts/allocate.py sign:*)`, `Bash(python scripts/allocate.py apply:*)` and a `Read` rule
  for the private key file; I did not edit the permissions myself.
- Mistake made and repaired while building it: I overwrote the existing tracked
  `tests/test_allocation.py` (tests for the unrelated `risk/allocation.py`) with my tests. It was
  unmodified, so I restored it from git (no diff remains) and renamed my module and tests to
  `return_allocation` / `test_return_allocation.py`.

**OASIS run kit (2026-10-03), not yet run.** Seed `state/oasis_seeds/fc6ca01860ab.json` (validated against
`SimSeed`; **reconstructed** from the desk brief stored in the 10-03 database because no overlay journal
exists and the feed returns 410; the provenance is inside the seed). Budget: $1 cap, 5 agents, 2 steps,
Haiku 4.5; the runner's own doubled estimate for the whole run is about $0.08. `vm/run_oasis.cmd` is the
one-click script: copies the finished runner from the local edit history to `vm/oasis_runner_live.py`
(the file in git is an unfinished stub, and I left it and its tests alone), does a free dry run, asks you to
type YES, asks you to paste the key, checks it, runs, and then runs `vm/oasis_check.py`, which prints
whether the agents acted at all and, if they did not, the error OASIS logged. I did not run it: it needs
your key, and restoring that file was blocked for me. Afterwards: `state/oasis_inbox/result.json` and
`state/oasis_inbox/log/` (the swallowed errors); then `python scripts/oasis_sim.py ingest ...`.

**Two findings from building it.** (1) A `market --stressed_by--> domain` bridge hangs off no poll, so
it joins the main component only if that domain is also observed; a snapshot that never observes
`geopolitical` leaves an island. (2) The runner on the branch is the old stub; the newer runner exists
only in local edit history.

## 1. The gap, restated

`GRAPH_INFLUENCE_SCOPE.md` §1 measured the journal as two disconnected components: execution
(`analysis`, `symbol`, `venue`) and intel (`domain`, `event`, `market`, `poll`, `region`, `source`).
Adding a society layer without a join would create a **third** component. "Homogeneous" here means a
single connected component under one edge envelope, one weight convention and one provenance key
(`intent_id`), verified the way §1 was measured.

## 2. The chain, level by level

Each level writes edges into the same journal with the same envelope (`as_of` from the snapshot,
`influence` separate from `weight`, `meta` carrying the join keys).

| Level | Stage | Input | Output | Written as | Status |
|---|---|---|---|---|---|
| L0 | Observe | vendor snapshot, events | poll, event, region, source nodes | `observed`, `about_domain`, `mentioned_by` | exists (`intel/graph.py`). **Feed is down: WorldMonitor returned 410 on 2026-10-03, so L0 is empty today** |
| L1 | Society | a recorded L0 snapshot as seed | per-agent stance per symbol in [-1, +1] | `agent --holds_stance--> symbol` | runner exists; **LLM step unverified** (§7) |
| L2 | Factors + eigen | stance matrix S, symbol returns, factor series | filtered spectrum, loadings, eigenfactor risk | `symbol --loads_on--> factor` | pieces exist in four files (§3); **no named-factor data in the repo** |
| L3 | Overlay (reverse adapter) | society mean, dispersion, concentration | influence in (0, 1] per symbol | `poll --scaled--> symbol` | new; the join `GRAPH_INFLUENCE_SCOPE.md` §4 specifies |
| L4 | Curvature / energy | position convexity, rolled Greeks | Gaussian energy of the curvature polynomial | `symbol --curved_by--> poll`, meta only | `bsm_parallel_surface.py` (orders 2–3) exists; order 4 and the series-trust gate are in `bsm_spectral.py`, whose three sibling modules were not found (§7) |
| L5 | Cost | scaled notional, venue | round-trip cost ratio, accept or reject | `meta.round_trip_cost_ratio` on `scaled` | `VenueCostModel` and Router gate 11 exist on the real branch, **off by default** |
| L6 | Router | sized, cost-checked intent | accept / trim / reject | `intent_id` on the `scaled` edge | exists; the single gate, unchanged |
| L7 | Fill | broker fill | `traded` edge | `venue --traded--> symbol`, `influence=None` | exists |

### L1 — society
The runner seeds 5 to 8 personas with a "desk brief" post built from the seed's snapshot fields, steps
them, then asks the model for each agent's stance per symbol from their own posts. A stance an agent
never addressed is omitted, never zero-filled. The result carries per-symbol `mean`, `dispersion` and
`agents`.

### L2 — factors and eigen-structure
Three kinds of `factor` node, one node type, told apart by `meta.kind`:

| kind | Source | What it is |
|---|---|---|
| `named` | a supplied factor-return series (market, size, value, momentum, rates, vol) | regression loadings of each symbol. **No such series exists in the repo.** `bsm_spectral.py --validate` takes a `--factors` CSV, but its parser lives in the missing sibling modules, so the format is unconfirmed |
| `eigen` | MP-filtered correlation of symbol returns | eigenfactors; per-factor exposure `V'w` and variance contribution `λ·exposure²` (the `factor_risk` decomposition in `sp100_mp_portfolio_risk.py`) |
| `society` | top eigenvector of the stance matrix S | the direction the simulated society agrees on; its eigenvalue share is the *consensus concentration* |

### L3 — the reverse adapter (society weights → influence)
A pure function, pinned a priori, every parameter recorded in the edge `meta`:

```
adverse   = max(0, -mean_s)                       # only adverse consensus reduces
disperse  = clamp(dispersion_s / d_ref, 0, 1)     # disagreement = uncertainty
herd      = clamp(top_eig_share, 0, 1)            # consensus concentration, from L2
influence_s = clamp(1 - a*adverse - b*disperse*(1 - adverse) - c*herd, floor, 1.0)
```

- **One-sided by construction.** A constructive consensus gives `adverse = 0` and cannot push
  `influence_s` above 1.0, matching `test_overlay_only_ever_reduces`.
- **Applied once.** Society influence composes with the overlay scalar through a single product at one
  seam (the way `OverlaidBias` applies the overlay); both factors are written to the edge. No double
  counting.
- **`a`, `b`, `c`, `d_ref`, `floor` are not chosen here.** Frozen a priori in a small grid before any
  run is scored. Fitting them to P&L is the excluded feedback loop.

### L4 — Greek / curvature energy
`bsm_parallel_surface.py` implements `curvature_energy` = the mean squared nonlinear response of
call + put per unit budget through order 3. `bsm_spectral.py` adds the exact Gaussian energy
`E[(z'Mz/2)^2] = ((tr M)^2 + 2 tr M^2)/4`, principal curvatures, order 4 and a series-trust gate that
says "reprice" when the Taylor series is not contracting. Both files state in their own words that
**curvature energy is a proposed diagnostic, not a validated trading signal.** So:

- L4 is a **risk measure** for convex positions, never an entry signal.
- **A spot or crypto position has zero curvature, so L4 is the identity for the whole current book.**
  Options are not wired (`NEXT_SESSION.md`: no symbol grammar, no chain walk, no Greeks on `Quote`,
  multiplier missing from the paper fill path, sizer blind to premium and delta). L4 is specified so
  the chain is complete, implemented as a pure function on a supplied position, and left unconnected to
  orders until an options path exists.

### L5 — cost
Cost is a gate, not a signal, and it must sit **after** the influence scaling:
- `VenueCostModel.for_venue()` prices one side per venue: Questrade flat $4.95/fill, Kraken ~26 bps
  taker, IB per-share with a $1 order minimum and a 1% cap. Router gate 11
  (`max_round_trip_cost_ratio`, entries only, off by default) already runs after the size-cap trim.
- **The interaction this scope has to respect:** scaling a position by `influence = 0.5` halves its
  notional and therefore **doubles its fixed-fee cost ratio** on Questrade (a $4.95 fee on $1,000 is
  0.5% per side; on $500 it is 1%). So the cost gate evaluates the *scaled* intent, and an intent that
  falls under the cost ceiling is **rejected, never sized back up** (the existing rule in the
  cost-aware-sizing item). The rejection names the number.
- **Measured constraint to carry:** Kraken's taker fee alone is ~52 bps round trip, so a flat 0.5%
  ceiling fits at no size there; ceilings are per venue (or ~2% on Kraken), chosen from a replay of
  closed round trips, not from the $4.95 arithmetic.
- Paper fills also carry the broker model's 5 bps slippage and, where enabled, a spread ceiling (50 bps
  equity / 30 bps crypto). The cost the graph records is the modelled one; it is not a measured fill
  quality.
- Cost energy comparison: the curvature energy of a convex leg is only comparable to its round-trip
  cost once premium is on the same scale (energy is per unit budget). Not meaningful for spot.

## 3. What the four spectral/curvature files contribute, and what not to import

| File | Reuse (extract as pure functions into the repo) | Do not |
|---|---|---|
| `financial_spectral_filters.py` | `robust_covariance` (sample or Ledoit-Wolf, then correlation), `spectral_decomposition`, `marchenko_pastur_bounds`, `classify_spectrum` (bulk fraction, structural-factor fraction, largest-to-second ratio, effective rank), eigenvector localization | Its 50-distribution registry is a research harness. Its `tracy_widom_proxy` is a finite-sample approximation and stays a diagnostic. The file silences all warnings globally |
| `sp100_mp_portfolio_risk.py` | `mp_bounds`, `eigendecompose`, `mp_filter_correlation` (clip noise eigenvalues to their mean, renormalize to unit diagonal), `correlation_to_covariance`, `risk_contribution`, `factor_risk` | Run it as a script. It scrapes Wikipedia for tickers and downloads prices from Yahoo at run time |
| `bsm_parallel_surface.py` | `State`, `prices`, `greeks`, `taylor_terms`, `curvature_energy` (orders 2–3), `parity_residual`, derivative checks | Treat Taylor prices as bounded; its docstring says never to clip violations away |
| `bsm_spectral.py` | curvature-risk vector, Gaussian energy by order, covariance filters (rolling, EWMA, eigenvalue floor) compared by VaR coverage, hierarchical vs complete-sum hedging | Run it as-is: it imports `bsm_parallel_surface_4th`, `bsm_signal` and `bsm_direction`, none of which were found |

**Three Marchenko-Pastur implementations now exist** (`analysis/rmt.py` with a Tracy-Widom margin and
trace-preserving noise; the clip-to-mean filter above; the bare bounds). They give different matrices.
**Pin exactly one**, record its estimator, σ² and lower cut in every `loads_on` edge's `meta`, and keep
the others as comparison runs. The repo's own history records the σ² fixed-point iteration collapsing
1 → 10 signal modes once.

## 4. Making the journal homogeneous

1. **One envelope.** Every edge has `as_of` (from the snapshot), `weight` in its native per-predicate
   unit, `influence` in (0, 1] or `None`, and `meta`.
2. **New node types:** `agent`, `factor`, `society` (the run). **New predicates:** `holds_stance`,
   `loads_on`, `scaled`, `curved_by`. Each is added to the `Literal` and to `DEFAULT_POLICIES`. `scaled`
   gets no decay policy (a statement about a decision made at a moment); `holds_stance` and `loads_on`
   decay fast.
3. **One join key.** `meta.intent_id` on `scaled`, matching the id minted at `OrderIntent`
   construction and already inside the signed canonical bytes. A fill resolves through `scaled` to a
   poll, its events and sources, the society run and its agents, the factors the symbol loads on, and
   the cost ratio that was checked.
4. **Run identity.** `society` nodes carry the OASIS `run_id`; a result is ingested only against a seed
   id this repo issued.
5. **Ledger.** New events `SOCIETY_RESULT_INGESTED` and `INFLUENCE_APPLIED` in the hash-chained
   ledger, so the record states which society run resized which intent.

## 5. Simulating across the agent field

Three runs, in order. Nothing before the last is evidence about trading.

- **A. Synthetic plumbing.** A seeded RNG generates a stance matrix, a return correlation matrix and a
  factor series; push them through L2 → L5 → a paper intent and assert one connected component,
  `influence ≤ 1`, and cost evaluated after scaling. Every output is labelled SYNTHETIC. This proves
  wiring, not edge.
- **B. Replay on recorded snapshots.** Seeds built from journaled L0 snapshots, never invented
  readings; a small field first, then larger. Spend is capped by the seed budget; the real ledger is
  the Anthropic console, and the runner's own figure is an estimate (about $0.01 per agent-step after
  its 2× safety factor).
- **C. Paired paper A/B.** Two books, same symbols, same time: with and without the society influence,
  for the standing 4 weeks, scored on `sortino_over_dd` with Sortino, return, time in market and trade
  count beside it, net of costs and still ahead at 2× costs.

## 6. Invariants this inherits (do not reopen)

- Intelligence may reduce size and never increase it; `(0, 1]` is enforced at construction.
- No feedback from outcomes onto weights. The base is 7 closed round trips at a flat $4.95 per fill; a
  fitted loop would learn the fee schedule.
- `traded` carries `influence=None`, permanently.
- Every order crosses `execution.router.Router`; paper only; the card gate stays on top of the risk
  gate. Live mode is a human decision.
- Data-first: one pass validates the protocol and promotes nothing. A graph write never breaks a
  trading step.
- Stance is an LLM's reading of simulated posts, a hypothesis generator and not a measurement.
  Curvature energy is a diagnostic by its authors' own description.

## 7. Blockers and honest unknowns

1. **The LLM step has never worked.** The 2026-10-03 run executed two steps and produced **zero**
   posts, comments or likes beyond the seed. **Correction:** I first read the database's "00:37 and 00:39"
   timestamps as two real minutes. They are OASIS's simulated clock, which runs 60x faster than real time
   (`Platform` builds `Clock(60)`), so the whole run took about three real seconds: ten LLM calls
   returning immediately, which fits an authentication or workspace error far better than a slow model.
   OASIS swallows that error by design: `SocialAgent.perform_action_by_llm` catches every exception and
   writes it to `./log/social.agent-<time>.log` in the process's working folder, and the module will not
   even import unless that `log` folder exists. The log from the 10-03 run is not on this machine. The
   next run keeps it: `vm/run_oasis.cmd` runs from `state/oasis_inbox` with a `log` folder, so the real
   cause will be on disk afterwards. camel passes extra client arguments through to the Anthropic client,
   so the workspace header in the recovered runner does reach it.
2. **Five agents cannot support L2.** The stance matrix is at most 5 × 6: rank ≤ 4, and an MP edge on
   so few observations is noise. A usable spectrum needs tens of agents and repeated steps, which also
   multiplies spend.
3. **No named-factor data.** Nothing in the repo supplies market/size/value/momentum series. Either a
   vendor-free construction from cached returns (long-short portfolios on the traded universe — which is
   small and unrepresentative) or a supplied file. Neither exists.
4. **Personas are hand-set.** Eight archetypes written by us encode our priors.
5. **L0 is down.** WorldMonitor returns 410, the energy source was already 140 hours stale, and the
   overlay journal is empty in the runnable checkout.
6. **L4 is inert for spot.** See §2.
7. **Missing modules:** `bsm_parallel_surface_4th`, `bsm_signal`, `bsm_direction`.
8. **Lost artifacts (corrected).** `scripts/oasis_sim.py` and `sim/contract.py` are **not** lost: they
   are on the `-wq7f42` branch, and `ingest_result` already rejects an errored or empty run, so today's
   silent failure could not have been stored as a result. What is lost: the original seed files and the
   journals. The newer runner is recoverable from local edit history only (see the Base note).
9. **Journals are gone.** No `state/` exists anywhere found, so the corpus counts quoted from
   `NEXT_SESSION.md` (275 Questrade fills, 14,000+ graph edges, 341 overlay rows) cannot be re-measured,
   and seeds can only be built from a fresh overlay row, which needs the WorldMonitor feed (410 today).

## 8. Acceptance

1. After a society run is ingested, the journal's type graph is **one connected component**, measured as
   in `GRAPH_INFLUENCE_SCOPE.md` §1.
2. A fill's `intent_id` resolves through `scaled` to a poll, its events and sources, the society run and
   agents, its `factor` nodes (each with `meta.kind`), and the checked cost ratio.
3. `influence` never exceeds 1.0 for any input, including a unanimously constructive society.
4. A society with no adverse consensus leaves size unchanged except for the dispersion and herd terms,
   which only reduce.
5. Society influence and the overlay scalar compose by one product, both factors on the edge.
6. The cost check runs on the scaled intent; an intent pushed under the ceiling by scaling is rejected,
   with the ratio in the reason, and is never resized upward.
7. Exactly one MP estimator is in use, and its parameters are in `meta`.
8. `holds_stance` and `loads_on` decay; `scaled` and `traded` do not.
9. Run A passes with SYNTHETIC output labelled as such and no P&L figure anywhere.

## 9. Phases

| # | Phase | Depends on | Size |
|---|---|---|---|
| 0 | ~~Locate the real working tree~~ done: GitHub `feat/multi-scoring-attention-map-wq7f42` @ `97067ec`. Remaining: a durable checkout somewhere you choose, then re-check names against it | the user | small |
| 1 | `Edge.influence` + `scaled` + new node types/predicates + policies (`GRAPH_INFLUENCE_SCOPE.md` §3–4) | 0 | medium |
| 2 | Extract the pure spectral functions (§3) into one repo module; pin one MP estimator | 0 | medium |
| 3 | Seed + ingest already exist (`scripts/oasis_sim.py`, `sim/contract.py`); replace the branch's stub runner with the newer one and re-issue a seed | 0 | small |
| 4 | Make one `LLMAction` step visibly act on a $1 run; read the failure first | 3, a scoped API key | small, spends |
| 5 | L2 on a larger stance matrix and on returns; `factor` nodes of all three kinds | 1, 2, 4, factor data | medium |
| 6 | L3 reverse adapter as a pure function + tests | 1, 5 | small |
| 7 | L5: cost evaluated after scaling; choose per-venue ceilings from a replay of closed round trips | 1, the venue-cost model | small |
| 8 | Run A (synthetic) end to end | 6, 7 | small |
| 9 | L4 as a pure function on a supplied convex leg; no order wiring | the missing bsm modules | small |
| 10 | Run B (replay), then Run C (paired paper A/B, ≥ 4 weeks) | 4–8 | long |

## 10. Needs the user

1. ~~Where is the real working tree?~~ Answered: GitHub `-wq7f42` @ `97067ec`. Open: do you have the
   post-09-30 work or `state/` anywhere (another machine, a VM, a backup)? And where should the durable
   checkout go?
2. `a`, `b`, `c`, `d_ref`, `floor`: chosen by you, or by a small a-priori grid I propose?
3. Named factors: build long-short factor portfolios from cached returns, or supply a factor file?
4. Which MP estimator is the one: the repo's `analysis/rmt.py`, the clip-to-mean filter, or another?
5. May L4 stay unconnected to orders until an options path exists?
6. The Anthropic key for phase 4: workspace-scoped, entered by you in your own terminal.
7. Where are `bsm_parallel_surface_4th`, `bsm_signal` and `bsm_direction`?


## 11. Direction notes and open questions

The owner's day-to-day direction notes are kept in a private copy of this document, outside the
repository, because they quote personal messages. The engineering questions that remain open are:

1. British audits and transaction history: which UK record-keeping expectation, and is "transaction
   history" a screen, a CSV, or a PDF statement? (The ledger and projection exist; the export and any
   regime-specific field set do not. This repository makes no compliance claim for any regime.)
2. A "100 ms bridge": which two components it connects, and whether 100 ms is a latency budget or a
   polling period.
3. Reporting currency: should every book report in CAD, including crypto and US names?
4. Allocation: where the owner key lives, and what weight a real split would use (capital balances in
   cents). Until a key is generated and its public half is passed with `--owner`, nothing can be
   authorized.
5. A lottery-style reward mechanic on user returns is deliberately NOT built: a prize draw paid on returns
   is a regulated gambling-style mechanic (Canadian Criminal Code and provincial licensing; UK Gambling
   Act 2005) that rewards risk-taking in a system that treats every fill as a cost.
