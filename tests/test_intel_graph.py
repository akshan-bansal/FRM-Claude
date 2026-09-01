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
