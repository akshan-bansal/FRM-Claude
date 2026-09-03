"""Append-only edge journal — the first step toward a graph-shaped intel record.

``intel/history.py`` derives change / relative position / persistence from a *flat* per-field time
series. That is enough for one field ("has strategic_risk been elevated for N reads") but cannot
answer anything relational: "has the *same domain-region pair* stayed hot", "which sources are
corroborating each other on this thread". Those are graph queries, and the flat JSONL cannot serve
them.

This module writes a second journal, ``state/intel_graph.jsonl``, alongside the existing overlay
journal. Every snapshot is decomposed into observations of the form ``(subject, predicate, object,
weight, ts)`` — one edge per row. The vendor already aggregates raw events into per-domain and
per-region counts, so the MVP's edges are those aggregates rather than individual event records;
richer per-event decomposition is the follow-up when we start consuming the raw ``events`` archive
directly. Even the aggregate form unlocks queries the flat frame cannot express, most importantly
per-edge persistence.

Design choices worth naming:

- Append-only. The graph is derived on read from the log, never mutated in place. This keeps every
  write O(1), keeps the record auditable, and makes the file safe to tail from another process.
- JSONL, not a graph store. SQLite / Neo4j are the honest next step once cross-edge queries get
  interesting; for the MVP a JSONL is enough and matches the rest of ``state/``.
- ``as_of`` on every edge, from the *snapshot*, not wall-clock. The snapshot is the point-in-time
  record; recomputing "when" from ``datetime.now()`` at write time would corrupt back-fills.
- Failures never propagate. Journaling is fire-and-forget; a bad write must not break a trading
  loop. Same contract as ``intel/history.py::append_snapshot``.

**Relationship to OASIS.** OASIS's ``agent_graph`` is opaque simulation state — no external adapter
hooks, edge types implicit in the ``ActionType`` enum, agent nodes indexed by integer, mutation
through ``env.step(actions)`` on a whole batch. Its shape transfers (typed predicate as a Literal;
node identity as a ``(type, id)`` tuple; the snapshot decomposition as an atomic batch write) but
its runtime does not — the graph here is queryable and external by design, because it is journal,
not simulation.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from trading_live_claude.intel.overlay import IntelSnapshot
from trading_live_claude.logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_GRAPH_JOURNAL = "state/intel_graph.jsonl"

# Node types. ``event`` was added when per-event decomposition landed — before that, edges only
# saw the vendor's already-aggregated fields. ``venue`` and ``symbol`` were added when the paper
# brokers started closing the fills→graph loop, giving downstream queries like "which venues
# traded X" or "did fills cluster around a stressed_by market bridge" first-class node targets.
NodeType = Literal[
    "poll", "domain", "region", "source", "market", "event", "venue", "symbol",
]

# Edge predicates.
#   ``observed`` — a poll observed a domain / region / source at some weight
#   ``elevated_in`` — the domain has an event-acceleration ratio above 1.0
#   ``co_occurs`` — two domains both elevated in the same poll
#   ``stressed_by`` — a market bridge (commodity ← energy, global ← geopolitical)
#   ``mentioned_by`` — an event has a source that reported it (feeds corroboration counts)
#   ``about_domain`` — an event categorized under a domain
#   ``affects_region`` — an event associated with a specific region/country
#   ``traded`` — a venue executed a fill on a symbol; weight is the notional, meta carries
#                action / qty / price / session_id / order_id / ts_fill (a per-fill decomposition,
#                paired with venue and symbol nodes to keep the record queryable)
Predicate = Literal[
    "observed", "elevated_in", "co_occurs", "stressed_by",
    "mentioned_by", "about_domain", "affects_region", "traded",
]

# Threshold below which "elevated" is not asserted. Matches the interpret.py convention that a
# domain acceleration under 2.0 is not evidence, and keeps single-wire noise out of the graph.
ELEVATION_THRESHOLD = 2.0


@dataclass(frozen=True)
class Edge:
    """One observed relationship, timestamped at the snapshot it came from.

    ``subject`` and ``object`` are ``(node_type, id)`` pairs so the same string ("energy") means
    different things in different contexts (as a domain vs. as a commodity) without a global
    disambiguation table.
    """

    subject: tuple[NodeType, str]
    predicate: Predicate
    object: tuple[NodeType, str]
    weight: float = 1.0
    as_of: str = ""        # ISO-8601 UTC; set by the writer from snapshot.as_of
    meta: dict[str, float | str] = field(default_factory=dict)

    def to_row(self) -> dict[str, object]:
        return {
            "subject": [self.subject[0], self.subject[1]],
            "predicate": self.predicate,
            "object": [self.object[0], self.object[1]],
            "weight": self.weight,
            "as_of": self.as_of,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_row(cls, row: dict[str, object]) -> "Edge":
        s = row["subject"]
        o = row["object"]
        assert isinstance(s, list) and isinstance(o, list)
        return cls(
            subject=(s[0], s[1]),           # type: ignore[arg-type]
            predicate=row["predicate"],      # type: ignore[arg-type]
            object=(o[0], o[1]),            # type: ignore[arg-type]
            weight=float(row.get("weight", 1.0)),      # type: ignore[arg-type]
            as_of=str(row.get("as_of", "")),
            meta=dict(row.get("meta") or {}),      # type: ignore[arg-type]
        )


def snapshot_to_edges(snap: IntelSnapshot, poll_id: str | None = None) -> list[Edge]:
    """Decompose a snapshot into edges. Aggregated fields only — no free-text extraction.

    ``poll_id`` identifies the emitting read (e.g. an ISO timestamp) so persistence queries can
    group edges by their originating poll without joining on ``as_of`` strings.
    """
    ts = snap.as_of.isoformat() if snap.as_of else ""
    pid = poll_id or ts
    poll: tuple[NodeType, str] = ("poll", pid)
    out: list[Edge] = []

    # Domain observations. Category alert counts are the aggregate the vendor already surfaces.
    for dom, count in (snap.category_alert_counts or {}).items():
        if count <= 0:
            continue
        out.append(Edge(poll, "observed", ("domain", str(dom)), weight=float(count), as_of=ts))

    # Event-acceleration edges: the domain is elevated if its ratio clears the threshold.
    elevated_domains: list[str] = []
    for dom, accel in (snap.event_acceleration or {}).items():
        if accel is None:
            continue
        if float(accel) >= ELEVATION_THRESHOLD:
            elevated_domains.append(str(dom))
            out.append(Edge(poll, "elevated_in", ("domain", str(dom)),
                            weight=float(accel), as_of=ts,
                            meta={"threshold": ELEVATION_THRESHOLD}))

    # Co-occurrence of elevated domains, for later corroboration queries. Undirected, so we write
    # both directions to keep read-side queries symmetric without a special case.
    for i, a in enumerate(elevated_domains):
        for b in elevated_domains[i + 1:]:
            out.append(Edge(("domain", a), "co_occurs", ("domain", b), as_of=ts,
                            meta={"poll": pid}))
            out.append(Edge(("domain", b), "co_occurs", ("domain", a), as_of=ts,
                            meta={"poll": pid}))

    # Region observations. Country alert counts, one edge each.
    for region, count in (snap.country_alert_counts or {}).items():
        if count <= 0:
            continue
        out.append(Edge(poll, "observed", ("region", str(region)), weight=float(count), as_of=ts))

    # Source ages. Recorded so freshness is queryable from the graph as well as the overlay.
    for src, age_h in (snap.source_age_hours or {}).items():
        out.append(Edge(("source", str(src)), "observed", poll, weight=float(age_h), as_of=ts,
                        meta={"kind": "age_hours"}))

    # Market stress bridges — commodity-class stress driven by energy, etc. These are the scalars
    # the overlay already reads; carrying them into the graph lets a persistence query say "energy
    # stress has been driving commodity de-risk for N polls" without reloading the overlay journal.
    if snap.energy_stress and snap.energy_stress > 0.0:
        out.append(Edge(("market", "commodity"), "stressed_by", ("domain", "energy"),
                        weight=float(snap.energy_stress), as_of=ts))
    if snap.strategic_risk and snap.strategic_risk >= 60.0:
        out.append(Edge(("market", "global"), "stressed_by", ("domain", "geopolitical"),
                        weight=float(snap.strategic_risk), as_of=ts))

    return out


def event_records_to_edges(
    records: Sequence[dict[str, object]],
    *,
    domain: str,
    poll_id: str,
    as_of: str,
) -> list[Edge]:
    """Decompose raw vendor event records into per-event edges.

    Each record produces one ``event`` node identified by whichever id the vendor gave it
    (falling back to a hash of title + timestamp so the node is stable across polls). From that
    node we write, when the fields are present:

    * ``event`` -- ``mentioned_by`` --> ``source`` for each source in the record. This is what
      makes corroboration a queryable graph property rather than a boolean flag from the vendor.
    * ``event`` -- ``about_domain`` --> ``domain`` for the categorized domain. When the record
      names its own categories we use those; otherwise we fall back to the ``domain`` argument
      (i.e. which archive we pulled the record from).
    * ``event`` -- ``affects_region`` --> ``region`` for country/region codes on the record.

    Records with no identifiable event id AND no title are skipped rather than fabricated — an
    edge without a stable subject is worse than no edge.
    """
    out: list[Edge] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        ev_id = _event_id(rec)
        if ev_id is None:
            continue
        ev_node: tuple[NodeType, str] = ("event", ev_id)

        # sources → mentioned_by. Each source is one edge; corroboration = distinct sources per
        # event, cheaply countable with edges_where afterwards.
        for src in _extract_sources(rec):
            out.append(Edge(ev_node, "mentioned_by", ("source", src),
                            as_of=as_of, meta={"poll": poll_id}))

        # categories → about_domain. Vendor-declared categories win over the domain we pulled from.
        cats = _extract_categories(rec) or [domain]
        for cat in cats:
            out.append(Edge(ev_node, "about_domain", ("domain", cat),
                            as_of=as_of, meta={"poll": poll_id}))

        # region/country → affects_region.
        for region in _extract_regions(rec):
            out.append(Edge(ev_node, "affects_region", ("region", region),
                            as_of=as_of, meta={"poll": poll_id}))

        # And a poll → event observation so downstream persistence queries can group by poll.
        out.append(Edge(("poll", poll_id), "observed", ev_node, as_of=as_of))
    return out


# ---- temporal gate: prune + wash ---------------------------------------------
# The graph journal grows monotonically — every poll appends, nothing is ever removed. Over weeks
# that becomes: an event mentioned six months ago is still counting toward corroboration; a
# co-occurrence from an unrelated regime is still an edge. The temporal gate below fixes both:
# **prune** drops edges past a hard TTL, and **wash** multiplicatively decays their ``weight``
# with age so old edges contribute less to weighted queries without going away entirely.
#
# Policies are per-predicate so different edge types can decay at different rates. Reasonable
# defaults reflect what each predicate actually means: source mentions are long-memory (a news
# outlet reporting on an event stays a source months later), co-occurrences are short-memory
# (they describe a specific regime), event->domain edges are medium (categorization is stable
# while the event is fresh).

from dataclasses import replace as _dc_replace
from datetime import UTC, datetime as _datetime, timedelta as _timedelta


@dataclass(frozen=True)
class DecayPolicy:
    """One temporal decay + prune rule for a single predicate.

    ``mode`` picks the curve:
      * ``"exp"`` — exponential ``w_out = w * 0.5 ** (age_h / half_life_h)``
      * ``"step"`` — piecewise: ``w * 1.0`` in the first band, ``* mid_factor`` in the second,
        ``* tail_factor`` after that. Bands set by ``step_band1_h`` / ``step_band2_h``.
      * ``"linear"`` — ``w * max(0, 1 - age_h/full_decay_h)``
      * ``"none"`` — no decay, only pruning is applied

    ``ttl_h`` prunes edges older than this many hours (``None`` = no hard TTL, keep everything
    that survives the decay). Set both together to control shape and age cap.
    """

    mode: Literal["exp", "step", "linear", "none"] = "exp"
    half_life_h: float = 24.0                # exp only
    step_band1_h: float = 24.0               # step only
    step_band2_h: float = 168.0              # step only
    step_mid_factor: float = 0.5             # step only
    step_tail_factor: float = 0.1            # step only
    full_decay_h: float = 168.0              # linear only
    ttl_h: float | None = None
    min_weight: float = 0.01                 # drop after decay if below this


# Default per-predicate policies. Overrideable; a caller can pass a custom map to wash_edges.
DEFAULT_POLICIES: dict[Predicate, DecayPolicy] = {
    "observed":       DecayPolicy(mode="exp", half_life_h=72.0, ttl_h=30 * 24),
    "elevated_in":    DecayPolicy(mode="exp", half_life_h=24.0, ttl_h=14 * 24),
    "co_occurs":      DecayPolicy(mode="linear", full_decay_h=48.0, ttl_h=14 * 24),
    "stressed_by":    DecayPolicy(mode="exp", half_life_h=24.0, ttl_h=14 * 24),
    "mentioned_by":   DecayPolicy(mode="exp", half_life_h=168.0, ttl_h=90 * 24),
    "about_domain":   DecayPolicy(mode="step", step_band1_h=48.0, step_band2_h=336.0,
                                    step_mid_factor=0.6, step_tail_factor=0.2, ttl_h=60 * 24),
    "affects_region": DecayPolicy(mode="step", step_band1_h=48.0, step_band2_h=336.0,
                                    step_mid_factor=0.6, step_tail_factor=0.2, ttl_h=60 * 24),
}


def _age_hours(edge: Edge, now: _datetime) -> float | None:
    """Positive age in hours from ``edge.as_of`` to ``now``. ``None`` if the stamp is unparseable."""
    if not edge.as_of:
        return None
    try:
        stamp = _datetime.fromisoformat(str(edge.as_of).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    delta = now - stamp
    return max(0.0, delta.total_seconds() / 3600.0)


def _decayed_weight(w: float, age_h: float, policy: DecayPolicy) -> float:
    """Apply the policy's decay curve to a weight at a given age."""
    if policy.mode == "none":
        return w
    if policy.mode == "exp":
        return w * float(0.5 ** (age_h / max(policy.half_life_h, 1e-9)))
    if policy.mode == "step":
        if age_h < policy.step_band1_h:
            return w
        if age_h < policy.step_band2_h:
            return w * policy.step_mid_factor
        return w * policy.step_tail_factor
    if policy.mode == "linear":
        f = max(0.0, 1.0 - age_h / max(policy.full_decay_h, 1e-9))
        return w * f
    return w   # unknown mode: no-op


def wash_edges(
    edges: Sequence[Edge],
    *,
    policies: dict[Predicate, DecayPolicy] | None = None,
    now: _datetime | None = None,
) -> list[Edge]:
    """Return a NEW list of edges with weights decayed and hard-TTL survivors only.

    Edges with an unparseable timestamp are kept unchanged (no way to know their age). Edges
    whose predicate has no policy are also kept unchanged. Never mutates the input.
    """
    # Distinguish "no argument" (use defaults) from "explicitly empty" (opt out of decay).
    pol_map = DEFAULT_POLICIES if policies is None else policies
    when = now or _datetime.now(UTC)
    out: list[Edge] = []
    for e in edges:
        pol = pol_map.get(e.predicate)
        if pol is None:
            out.append(e)
            continue
        age_h = _age_hours(e, when)
        if age_h is None:
            out.append(e)
            continue
        if pol.ttl_h is not None and age_h > pol.ttl_h:
            continue                    # pruned by hard TTL
        new_w = _decayed_weight(e.weight, age_h, pol)
        if new_w < pol.min_weight:
            continue                    # decayed below the noise floor
        if abs(new_w - e.weight) < 1e-12:
            out.append(e)
        else:
            out.append(_dc_replace(e, weight=new_w))
    return out


def wash_journal_file(
    path: str | Path = DEFAULT_GRAPH_JOURNAL,
    *,
    policies: dict[Predicate, DecayPolicy] | None = None,
    now: _datetime | None = None,
    backup: bool = True,
) -> dict[str, int]:
    """Rewrite the journal in place with wash_edges applied. Returns a before/after summary.

    Atomically writes to a ``.washing`` sibling then swaps in, so a crash mid-write cannot
    corrupt the journal. When ``backup=True`` (default) the pre-wash file is preserved at
    ``<path>.bak`` so the last wash is always undoable.
    """
    p = Path(path)
    if not p.exists():
        return {"before": 0, "after": 0, "pruned": 0}
    before_edges = load_edges(p)
    after_edges = wash_edges(before_edges, policies=policies, now=now)

    tmp = p.with_suffix(p.suffix + ".washing")
    with tmp.open("w", encoding="utf-8") as fh:
        for e in after_edges:
            fh.write(json.dumps(e.to_row(), default=str) + chr(10))
    if backup:
        bak = p.with_suffix(p.suffix + ".bak")
        try:
            if bak.exists():
                bak.unlink()
            p.replace(bak)
        except OSError:
            pass
    tmp.replace(p)
    return {"before": len(before_edges), "after": len(after_edges),
            "pruned": len(before_edges) - len(after_edges)}


def append_edges(edges: Iterable[Edge], path: str | Path = DEFAULT_GRAPH_JOURNAL) -> None:
    """Append a batch of edges to the graph journal. Never raises."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            for e in edges:
                fh.write(json.dumps(e.to_row(), default=str) + chr(10))
    except Exception:
        log.warning("intel.graph.append_failed", path=str(path))


def append_snapshot_edges(snap: IntelSnapshot, path: str | Path = DEFAULT_GRAPH_JOURNAL) -> None:
    """Convenience: decompose ``snap`` into edges and append them all."""
    append_edges(snapshot_to_edges(snap), path=path)


def fill_edge(*, venue: str, symbol: str, action: str, quantity: float, price: float,
              session_id: str, order_id: int | str, as_of: str) -> Edge:
    """Compose one ``traded`` edge for a fill. Weight is signed notional so downstream queries
    can distinguish net buying vs. net selling by venue-symbol without joining back to ``meta``.

    Kept as a plain constructor rather than a class method so the paper broker can call it without
    importing the graph module's dataclass surface directly — the fills journal must never crash a
    trading step, so the caller wraps ``append_edges([fill_edge(...)])`` in its own try/except.
    """
    sign = 1.0 if action.upper().startswith("B") else -1.0
    notional = float(quantity) * float(price)
    return Edge(
        subject=("venue", venue),
        predicate="traded",
        object=("symbol", symbol),
        weight=round(sign * notional, 4),
        as_of=as_of,
        meta={
            "action": action, "qty": float(quantity), "price": float(price),
            "session_id": session_id, "order_id": str(order_id),
        },
    )


def load_edges(path: str | Path = DEFAULT_GRAPH_JOURNAL) -> list[Edge]:
    """Load all edges from the graph journal, oldest-first (append order)."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[Edge] = []
    for line in p.read_text(encoding="utf-8").strip().splitlines():
        if not line.strip():
            continue
        try:
            out.append(Edge.from_row(json.loads(line)))
        except Exception:
            log.warning("intel.graph.load_skipped_row")
    return out


# ---- queries -----------------------------------------------------------------


def edges_where(edges: Sequence[Edge], *, subject: tuple[NodeType, str] | None = None,
                predicate: Predicate | None = None,
                object: tuple[NodeType, str] | None = None) -> list[Edge]:
    """Simple filter. Returns edges matching ALL provided constraints."""
    out: list[Edge] = []
    for e in edges:
        if subject is not None and e.subject != subject:
            continue
        if predicate is not None and e.predicate != predicate:
            continue
        if object is not None and e.object != object:
            continue
        out.append(e)
    return out


def edge_persistence(edges: Sequence[Edge], *, predicate: Predicate,
                     object: tuple[NodeType, str]) -> int:
    """How many consecutive polls (ending at the most recent) contain this predicate → object edge?

    Consecutive means: order polls by their earliest ``as_of``, then count from the end while the
    predicate/object appears in the poll's edge set. Analogous to
    ``intel/history.py::_run_length`` but per-edge rather than per-scalar.

    Returns 0 on an empty graph or when the edge is absent from the latest poll.
    """
    if not edges:
        return 0

    # Group edges by poll (identified by the "poll" node an edge was recorded from, or by as_of when
    # no poll node participates in the edge — the market/domain bridges).
    poll_order: list[str] = []
    seen_polls: set[str] = set()
    per_poll: dict[str, list[Edge]] = {}
    for e in edges:
        poll_id = None
        if e.subject[0] == "poll":
            poll_id = e.subject[1]
        elif e.object[0] == "poll":
            poll_id = e.object[1]
        else:
            poll_id = e.as_of        # bridges and co_occurs fall back to timestamp grouping
        if not poll_id:
            continue
        if poll_id not in seen_polls:
            seen_polls.add(poll_id)
            poll_order.append(poll_id)
        per_poll.setdefault(poll_id, []).append(e)

    if not poll_order:
        return 0

    run = 0
    for poll_id in reversed(poll_order):
        matched = False
        for e in per_poll[poll_id]:
            if e.predicate == predicate and e.object == object:
                matched = True
                break
        if not matched:
            break
        run += 1
    return run


# ---- per-event helpers -------------------------------------------------------
# These read the vendor's shape defensively — different tools nest fields differently, so we
# probe several likely names for each attribute and take the first that resolves.


def _event_id(rec: dict[str, object]) -> str | None:
    """Prefer the vendor's own id; otherwise a stable hash of title + timestamp."""
    import hashlib
    for key in ("id", "eventId", "signalId", "storyId", "uuid"):
        v = rec.get(key)
        if isinstance(v, (str, int)) and str(v):
            return str(v)
    title = rec.get("title") or rec.get("headline") or rec.get("summary")
    stamp = rec.get("ingestedAt") or rec.get("publishedAt") or rec.get("occurredAt")
    if title and stamp:
        return hashlib.sha1(f"{title}|{stamp}".encode(), usedforsecurity=False).hexdigest()[:16]
    return None


def _extract_sources(rec: dict[str, object]) -> list[str]:
    """Pull source names — handles list-of-strings, list-of-dicts, or a nested ``sources`` field."""
    raw = rec.get("sources") or rec.get("sourceList") or rec.get("outlets")
    if raw is None:
        one = rec.get("source") or rec.get("outlet")
        raw = [one] if one else []
    out: list[str] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str) and item:
                out.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("outlet") or item.get("id")
                if isinstance(name, str) and name:
                    out.append(str(name))
    return out


def _extract_categories(rec: dict[str, object]) -> list[str]:
    raw = rec.get("categories") or rec.get("tags") or rec.get("topics")
    if raw is None:
        one = rec.get("category") or rec.get("topic")
        raw = [one] if one else []
    out: list[str] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str) and item:
                out.append(item)
    return out


def recent_events_from_graph(
    edges: Sequence[Edge],
    *,
    limit: int = 40,
) -> list[dict[str, object]]:
    """Project recent event edges back into evidence records the agent layer can read.

    The graph journal decomposes each event into typed edges, so a "record" for the agent is
    reconstructed by joining the edges belonging to one event node: sources from ``mentioned_by``
    edges, categories from ``about_domain``, regions from ``affects_region``. Newest events
    first (by ``as_of``); at most ``limit`` events. Silent no-op when the graph has no events.
    """
    per_event: dict[str, dict[str, object]] = {}
    stamps: dict[str, str] = {}
    for e in edges:
        subj = e.subject if e.subject[0] == "event" else (
            e.object if e.object[0] == "event" else None)
        if subj is None:
            continue
        ev_id = subj[1]
        rec = per_event.setdefault(ev_id, {"id": ev_id, "sources": [], "categories": [],
                                             "regions": []})
        # Track the most recent as_of for this event so we can order records by it.
        if e.as_of and (ev_id not in stamps or e.as_of > stamps[ev_id]):
            stamps[ev_id] = e.as_of
        if e.predicate == "mentioned_by":
            src_list = rec["sources"]
            assert isinstance(src_list, list)
            if e.object[1] not in src_list:
                src_list.append(e.object[1])
        elif e.predicate == "about_domain":
            cats = rec["categories"]
            assert isinstance(cats, list)
            if e.object[1] not in cats:
                cats.append(e.object[1])
        elif e.predicate == "affects_region":
            regs = rec["regions"]
            assert isinstance(regs, list)
            if e.object[1] not in regs:
                regs.append(e.object[1])
    for ev_id, ts in stamps.items():
        per_event[ev_id]["ingestedAt"] = ts
    ordered = sorted(per_event.values(),
                      key=lambda r: str(r.get("ingestedAt") or ""), reverse=True)
    return ordered[:limit]


def _extract_regions(rec: dict[str, object]) -> list[str]:
    raw = rec.get("countries") or rec.get("regions") or rec.get("locations")
    if raw is None:
        one = rec.get("country") or rec.get("region") or rec.get("sourceCountry")
        raw = [one] if one else []
    out: list[str] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str) and item:
                out.append(item.upper())
            elif isinstance(item, dict):
                cc = item.get("code") or item.get("iso") or item.get("country")
                if isinstance(cc, str) and cc:
                    out.append(cc.upper())
    return out
