"""Tests for intel/graph.py — the append-only edge journal MVP."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading_live_claude.intel.graph import (
    ELEVATION_THRESHOLD,
    Edge,
    append_snapshot_edges,
    edge_persistence,
    edges_where,
    event_records_to_edges,
    load_edges,
    snapshot_to_edges,
)
from trading_live_claude.intel.overlay import IntelSnapshot


def _snap(**overrides: object) -> IntelSnapshot:
    """Build a snapshot with sane defaults; overrides win."""
    base: dict[str, object] = {
        "category_alert_counts": {"economy": 3, "conflict": 2},
        "country_alert_counts": {"US": 4, "CN": 1},
        "event_acceleration": {"energy": 3.0, "conflict": 1.2},
        "source_age_hours": {"news": 0.5, "energy": 48.0},
        "energy_stress": 0.4,
        "strategic_risk": 65.0,
        "as_of": datetime(2026, 8, 31, 18, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return IntelSnapshot(**base)          # type: ignore[arg-type]


def test_snapshot_decomposes_into_the_expected_edge_shape() -> None:
    edges = snapshot_to_edges(_snap())

    # Domain observations, one per non-zero category count.
    dom_obs = edges_where(edges, predicate="observed", object=("domain", "economy"))
    assert len(dom_obs) == 1 and dom_obs[0].weight == 3.0

    # Elevated_in only for domains at/above the threshold. energy=3.0 clears, conflict=1.2 does not.
    elev = edges_where(edges, predicate="elevated_in")
    assert {e.object[1] for e in elev} == {"energy"}

    # Region observations round-trip weights.
    us = edges_where(edges, predicate="observed", object=("region", "US"))
    assert us and us[0].weight == 4.0

    # Sources record their age via meta.
    src = edges_where(edges, subject=("source", "energy"))
    assert src and src[0].meta.get("kind") == "age_hours" and src[0].weight == 48.0

    # Stress bridges only fire above their thresholds (strategic_risk 65 >= 60 → geopolitical).
    geo = edges_where(edges, subject=("market", "global"), predicate="stressed_by")
    assert geo and geo[0].object == ("domain", "geopolitical")


def test_elevated_domains_write_symmetric_co_occurrence_edges() -> None:
    """Two elevated domains → two undirected co_occurs edges (a↔b, b↔a) so read-side is symmetric."""
    snap = _snap(event_acceleration={"energy": 3.0, "conflict": 4.0})
    edges = snapshot_to_edges(snap)
    co = edges_where(edges, predicate="co_occurs")
    pairs = {(e.subject[1], e.object[1]) for e in co}
    assert ("energy", "conflict") in pairs and ("conflict", "energy") in pairs


def test_below_threshold_domains_never_get_elevated_or_co_occurrence_edges() -> None:
    """A calm poll writes observations but no elevated_in / co_occurs edges."""
    calm = _snap(event_acceleration={"energy": 1.1, "conflict": 1.3})
    edges = snapshot_to_edges(calm)
    assert not edges_where(edges, predicate="elevated_in")
    assert not edges_where(edges, predicate="co_occurs")
    # observations should still be present, so the poll is on the record either way
    assert edges_where(edges, predicate="observed")


def test_stress_bridges_respect_their_thresholds() -> None:
    """energy_stress > 0 fires the commodity bridge; strategic_risk < 60 does NOT fire the geo one."""
    snap = _snap(strategic_risk=30.0)
    edges = snapshot_to_edges(snap)
    assert edges_where(edges, subject=("market", "commodity"), predicate="stressed_by")
    assert not edges_where(edges, subject=("market", "global"), predicate="stressed_by")


def test_edges_roundtrip_through_the_journal_file(tmp_path: Path) -> None:
    """A write followed by a read reproduces the exact edge set (byte-perfect on the fields)."""
    path = tmp_path / "graph.jsonl"
    snap = _snap()
    append_snapshot_edges(snap, path=path)
    loaded = load_edges(path=path)
    original = snapshot_to_edges(snap)
    assert len(loaded) == len(original)
    for a, b in zip(original, loaded, strict=True):
        assert a.subject == b.subject
        assert a.predicate == b.predicate
        assert a.object == b.object
        assert a.weight == b.weight
        assert a.as_of == b.as_of
        assert a.meta == b.meta


def test_edge_persistence_counts_consecutive_polls_ending_at_the_latest(tmp_path: Path) -> None:
    """The whole point of the graph: persistence is a per-edge query, not a per-field one."""
    path = tmp_path / "graph.jsonl"
    base_ts = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)

    # Three polls where energy is elevated, then one where it is not, then two more where it is.
    # The run counted from the latest is 2 (the trailing pair), NOT 5 or 6.
    schedule = [3.5, 3.2, 4.0, 1.1, 2.5, 3.0]
    for i, accel in enumerate(schedule):
        snap = _snap(as_of=base_ts + timedelta(hours=i), event_acceleration={"energy": accel})
        append_snapshot_edges(snap, path=path)

    edges = load_edges(path=path)
    run = edge_persistence(edges, predicate="elevated_in", object=("domain", "energy"))
    assert run == 2

    # A domain that was NEVER elevated has a persistence of 0.
    absent = edge_persistence(edges, predicate="elevated_in", object=("domain", "disaster"))
    assert absent == 0


def test_persistence_returns_zero_when_the_latest_poll_lacks_the_edge(tmp_path: Path) -> None:
    """Regression on the "run must end at the latest" invariant."""
    path = tmp_path / "graph.jsonl"
    base_ts = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)

    # elevated for three polls, then calm — the LATEST poll doesn't have the edge, so persistence = 0
    for i, accel in enumerate([3.0, 3.0, 3.0, 1.0]):
        snap = _snap(as_of=base_ts + timedelta(hours=i), event_acceleration={"energy": accel})
        append_snapshot_edges(snap, path=path)

    edges = load_edges(path=path)
    assert edge_persistence(edges, predicate="elevated_in", object=("domain", "energy")) == 0


def test_elevation_threshold_matches_interpret_convention() -> None:
    """A domain acceleration under 2.0 is not evidence — the same bar interpret.py uses."""
    assert ELEVATION_THRESHOLD == 2.0
    just_below = snapshot_to_edges(_snap(event_acceleration={"energy": 1.99}))
    just_at = snapshot_to_edges(_snap(event_acceleration={"energy": 2.0}))
    assert not edges_where(just_below, predicate="elevated_in")
    assert edges_where(just_at, predicate="elevated_in")


def test_append_never_raises_on_bad_path(tmp_path: Path) -> None:
    """Journaling is fire-and-forget — a bad write must not crash the caller."""
    # A path where the *parent* is a file, not a directory — cannot mkdir under it.
    bad_root = tmp_path / "not-a-dir"
    bad_root.write_text("x")
    bad_path = bad_root / "nested" / "graph.jsonl"
    # Should NOT raise.
    append_snapshot_edges(_snap(), path=bad_path)


def _make_edge(subj: tuple[str, str], pred: str, obj: tuple[str, str]) -> Edge:
    return Edge(subject=subj, predicate=pred, object=obj)      # type: ignore[arg-type]


# --- graph → agent evidence bundle ---------------------------------------------------------------

from trading_live_claude.intel.graph import recent_events_from_graph                  # noqa: E402


def test_recent_events_from_graph_reconstructs_records_from_typed_edges() -> None:
    """The debate loop consumes records; the journal stores edges. Round-trip must work."""
    edges = [
        Edge(subject=("event", "EV-1"), predicate="mentioned_by",
             object=("source", "Reuters"), as_of="2026-09-01T10:00:00+00:00"),
        Edge(subject=("event", "EV-1"), predicate="mentioned_by",
             object=("source", "Bloomberg"), as_of="2026-09-01T10:00:00+00:00"),
        Edge(subject=("event", "EV-1"), predicate="about_domain",
             object=("domain", "energy"), as_of="2026-09-01T10:00:00+00:00"),
        Edge(subject=("event", "EV-1"), predicate="affects_region",
             object=("region", "SA"), as_of="2026-09-01T10:00:00+00:00"),
        Edge(subject=("poll", "p1"), predicate="observed",
             object=("event", "EV-2"), as_of="2026-09-01T09:00:00+00:00"),
        Edge(subject=("event", "EV-2"), predicate="mentioned_by",
             object=("source", "AP"), as_of="2026-09-01T09:00:00+00:00"),
    ]
    recs = recent_events_from_graph(edges, limit=5)
    assert len(recs) == 2
    # Newest first
    first = recs[0]
    assert first["id"] == "EV-1"
    assert set(first["sources"]) == {"Reuters", "Bloomberg"}
    assert first["categories"] == ["energy"]
    assert first["regions"] == ["SA"]
    assert first["ingestedAt"] == "2026-09-01T10:00:00+00:00"


def test_recent_events_from_graph_dedupes_repeated_edges() -> None:
    """A source that shows up twice in the log should appear once in the reconstructed record."""
    edges = [
        Edge(subject=("event", "EV"), predicate="mentioned_by",
             object=("source", "Reuters"), as_of="t1"),
        Edge(subject=("event", "EV"), predicate="mentioned_by",
             object=("source", "Reuters"), as_of="t2"),
    ]
    recs = recent_events_from_graph(edges)
    assert recs[0]["sources"] == ["Reuters"]


# --- temporal gate: prune + wash ------------------------------------------------------------

from datetime import UTC, datetime as _dt, timedelta as _td            # noqa: E402

from trading_live_claude.intel.graph import (                          # noqa: E402
    DEFAULT_POLICIES,
    DecayPolicy,
    wash_edges,
    wash_journal_file,
)


def _aged_edge(pred: str, weight: float, hours_ago: float,
                now: _dt) -> Edge:
    when = now - _td(hours=hours_ago)
    return Edge(subject=("poll", "p"), predicate=pred,               # type: ignore[arg-type]
                object=("domain", "energy"),
                weight=weight, as_of=when.isoformat())


def test_exp_decay_halves_weight_at_the_half_life() -> None:
    """Exponential mode is the workhorse — one half-life = exactly 0.5x weight."""
    now = _dt(2026, 9, 1, tzinfo=UTC)
    e = _aged_edge("observed", 1.0, 72.0, now)      # default observed half-life = 72h
    (out,) = wash_edges([e], now=now)
    assert abs(out.weight - 0.5) < 1e-9


def test_step_decay_bands_apply_correct_factors() -> None:
    """step_band1 = 1.0x, step_band2 = mid_factor, tail = tail_factor."""
    now = _dt(2026, 9, 1, tzinfo=UTC)
    policies = {"about_domain": DecayPolicy(
        mode="step", step_band1_h=24.0, step_band2_h=168.0,
        step_mid_factor=0.6, step_tail_factor=0.2, ttl_h=None, min_weight=0.0)}
    fresh = _aged_edge("about_domain", 1.0, 5.0, now)          # in band1
    mid = _aged_edge("about_domain", 1.0, 48.0, now)           # in band2
    tail = _aged_edge("about_domain", 1.0, 400.0, now)         # past band2
    out = wash_edges([fresh, mid, tail], policies=policies, now=now)
    assert [o.weight for o in out] == [1.0, 0.6, 0.2]


def test_linear_decay_zeros_at_full_decay_and_drops_when_min_hit() -> None:
    now = _dt(2026, 9, 1, tzinfo=UTC)
    policies = {"co_occurs": DecayPolicy(mode="linear", full_decay_h=48.0,
                                          ttl_h=None, min_weight=0.01)}
    half = _aged_edge("co_occurs", 1.0, 24.0, now)
    stale = _aged_edge("co_occurs", 1.0, 47.5, now)
    dead = _aged_edge("co_occurs", 1.0, 48.5, now)
    out = wash_edges([half, stale, dead], policies=policies, now=now)
    weights = [o.weight for o in out]
    assert abs(weights[0] - 0.5) < 1e-9
    assert weights[1] < 0.05
    # ``dead`` decayed below min_weight AND past full_decay -> dropped
    assert len(out) == 2


def test_ttl_prunes_edges_past_the_cap_regardless_of_decay() -> None:
    """A hard TTL is a floor, not affected by decay — an old edge just goes away."""
    now = _dt(2026, 9, 1, tzinfo=UTC)
    policies = {"mentioned_by": DecayPolicy(mode="exp", half_life_h=168.0,
                                              ttl_h=24.0, min_weight=0.0)}
    fresh = _aged_edge("mentioned_by", 1.0, 12.0, now)
    old = _aged_edge("mentioned_by", 1.0, 48.0, now)
    out = wash_edges([fresh, old], policies=policies, now=now)
    assert len(out) == 1 and out[0].as_of == fresh.as_of


def test_edges_with_no_policy_pass_through_untouched() -> None:
    """A predicate that has no matching policy must not be silently discarded or reweighted."""
    now = _dt(2026, 9, 1, tzinfo=UTC)
    e = _aged_edge("mentioned_by", 1.0, 500.0, now)
    out = wash_edges([e], policies={}, now=now)         # empty policies map
    assert out == [e]


def test_edges_with_unparseable_timestamp_are_kept_as_is() -> None:
    """No way to know age — keep the edge unchanged rather than drop it."""
    now = _dt(2026, 9, 1, tzinfo=UTC)
    bad = Edge(subject=("poll", "p"), predicate="observed",
                object=("domain", "energy"), weight=1.0, as_of="not-a-timestamp")
    out = wash_edges([bad], now=now)
    assert out == [bad]


def test_default_policies_encode_the_predicate_memory_hierarchy() -> None:
    """Regression pin: sources > about_domain > observed > elevated_in > co_occurs."""
    assert DEFAULT_POLICIES["mentioned_by"].half_life_h > DEFAULT_POLICIES["observed"].half_life_h
    assert DEFAULT_POLICIES["observed"].half_life_h > DEFAULT_POLICIES["elevated_in"].half_life_h
    # co_occurs is linear so compare its full_decay to what observed would keep
    assert DEFAULT_POLICIES["co_occurs"].mode == "linear"
    assert DEFAULT_POLICIES["co_occurs"].full_decay_h < DEFAULT_POLICIES["observed"].half_life_h


def test_wash_journal_file_roundtrips_and_writes_a_backup(tmp_path: Path) -> None:
    """End-to-end: build a journal, wash it, verify pruning + backup."""
    now = _dt(2026, 9, 1, tzinfo=UTC)
    path = tmp_path / "graph.jsonl"
    edges = [_aged_edge("observed", 1.0, 12.0, now),        # keep (fresh)
             _aged_edge("observed", 1.0, 8 * 24, now)]      # keep (decayed, above min)
    # add one WAY-old that will be pruned by observed's 30-day TTL
    ancient = _aged_edge("observed", 1.0, 60 * 24, now)
    from trading_live_claude.intel.graph import append_edges as _ae
    _ae([*edges, ancient], path=path)

    summary = wash_journal_file(path, now=now)
    assert summary["before"] == 3
    assert summary["after"] == 2
    assert summary["pruned"] == 1
    assert (tmp_path / "graph.jsonl.bak").exists()          # backup was written


def test_recent_events_from_graph_returns_empty_when_no_event_edges() -> None:
    edges = [Edge(subject=("poll", "p"), predicate="observed",
                   object=("domain", "energy"), as_of="t")]
    assert recent_events_from_graph(edges) == []


# --- per-event decomposition ---------------------------------------------------------------------


def _sig(id_: str, sources: list[str], categories: list[str], country: str) -> dict[str, object]:
    """A cross-source-signal-shaped vendor record."""
    return {"id": id_, "sources": sources, "categories": categories, "country": country}


def test_event_records_produce_source_domain_and_region_edges() -> None:
    """Each record decomposes into mentioned_by + about_domain + affects_region."""
    recs = [_sig("EV-1", ["Reuters", "Bloomberg"], ["energy", "supply"], "SA")]
    edges = event_records_to_edges(recs, domain="news_signal",
                                    poll_id="p1", as_of="2026-09-01T00:00:00+00:00")

    # 2 sources
    mentions = edges_where(edges, subject=("event", "EV-1"), predicate="mentioned_by")
    assert {e.object[1] for e in mentions} == {"Reuters", "Bloomberg"}

    # 2 declared categories WIN over the domain fallback
    domains = edges_where(edges, subject=("event", "EV-1"), predicate="about_domain")
    assert {e.object[1] for e in domains} == {"energy", "supply"}

    # 1 region, uppercased
    regions = edges_where(edges, subject=("event", "EV-1"), predicate="affects_region")
    assert {e.object[1] for e in regions} == {"SA"}

    # poll -> event observation edge, so persistence queries can group per poll
    assert edges_where(edges, subject=("poll", "p1"), predicate="observed",
                       object=("event", "EV-1"))


def test_records_without_id_or_title_are_skipped_rather_than_fabricated() -> None:
    """An edge without a stable subject is worse than no edge."""
    edges = event_records_to_edges(
        [{"sources": ["Reuters"]}], domain="news_signal", poll_id="p", as_of="")
    assert edges == []


def test_records_fall_back_to_a_hash_of_title_and_timestamp_when_id_is_missing() -> None:
    """No vendor id, but title + timestamp are present — use the deterministic fallback id."""
    rec = {"title": "Refinery outage", "publishedAt": "2026-08-30T09:00:00Z",
           "sources": ["FT"]}
    edges = event_records_to_edges([rec], domain="news_signal", poll_id="p",
                                    as_of="2026-08-30T10:00:00Z")
    subj_ids = {e.subject[1] for e in edges if e.subject[0] == "event"}
    assert len(subj_ids) == 1
    # Same input twice yields the SAME id — stability across polls is the point.
    edges2 = event_records_to_edges([rec], domain="news_signal", poll_id="p2",
                                     as_of="2026-08-30T11:00:00Z")
    subj_ids2 = {e.subject[1] for e in edges2 if e.subject[0] == "event"}
    assert subj_ids == subj_ids2


def test_domain_fallback_fires_only_when_the_record_declares_none() -> None:
    """If the record itself carries categories, those win; the domain arg is only a fallback."""
    with_cats = _sig("A", ["Reuters"], ["fx"], "US")
    without_cats = {"id": "B", "sources": ["Reuters"], "country": "US"}
    e_a = event_records_to_edges([with_cats], domain="news_signal",
                                  poll_id="p", as_of="")
    e_b = event_records_to_edges([without_cats], domain="news_signal",
                                  poll_id="p", as_of="")
    assert {e.object[1] for e in edges_where(e_a, predicate="about_domain")} == {"fx"}
    assert {e.object[1] for e in edges_where(e_b, predicate="about_domain")} == {"news_signal"}


def test_source_extraction_handles_dict_shape() -> None:
    """Vendors sometimes serialize sources as a list of dicts with a ``name`` key."""
    rec = {"id": "EV", "sources": [{"name": "Reuters"}, {"outlet": "AP"}, {"id": "wsj"}]}
    edges = event_records_to_edges([rec], domain="d", poll_id="p", as_of="")
    names = {e.object[1] for e in edges_where(edges, predicate="mentioned_by")}
    assert names == {"Reuters", "AP", "wsj"}


def test_corroboration_is_now_a_graph_query_over_mentioned_by_edges() -> None:
    """The whole point: distinct sources per event = corroboration count, straight from the graph."""
    two_source = _sig("EV-A", ["Reuters", "Bloomberg"], ["news"], "US")
    single = _sig("EV-B", ["Reuters"], ["news"], "US")
    edges = (event_records_to_edges([two_source], domain="d", poll_id="p", as_of="")
             + event_records_to_edges([single], domain="d", poll_id="p", as_of=""))

    def _corroboration(event_id: str) -> int:
        return len({e.object[1]
                    for e in edges_where(edges, subject=("event", event_id),
                                          predicate="mentioned_by")})
    assert _corroboration("EV-A") == 2
    assert _corroboration("EV-B") == 1


def test_edges_where_is_conjunctive_over_provided_filters() -> None:
    """subject + predicate + object all constrain; omitted fields are wildcards."""
    edges = [
        _make_edge(("poll", "p1"), "observed", ("domain", "energy")),
        _make_edge(("poll", "p1"), "observed", ("domain", "conflict")),
        _make_edge(("poll", "p2"), "observed", ("domain", "energy")),
        _make_edge(("poll", "p1"), "elevated_in", ("domain", "energy")),
    ]
    assert len(edges_where(edges, predicate="observed")) == 3
    assert len(edges_where(edges, subject=("poll", "p1"))) == 3
    assert len(edges_where(edges, predicate="observed",
                           object=("domain", "energy"))) == 2
    assert len(edges_where(edges, subject=("poll", "p1"), predicate="elevated_in",
                           object=("domain", "energy"))) == 1
