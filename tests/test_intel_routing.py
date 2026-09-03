"""Tests for intel/routing.py — new coverage focuses on the persistence gate.

The classify_symbol paths and OverlayProvider are exercised elsewhere; this module covers the
Tier 3 cross-path gate: :class:`PersistenceGate` reading the graph, mapping symbols to their
overlay class's watched domains, and blocking entries when any of those domains has been
elevated for min_polls or more consecutive polls.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading_live_claude.intel.graph import Edge, append_edges
from trading_live_claude.intel.routing import (
    PersistenceGate,
    _CLASS_TO_DOMAINS,
    classify_symbol,
)


def _elevated_edge(poll_id: str, domain: str) -> Edge:
    """One 'domain elevated_in poll' edge — the shape edge_persistence looks for."""
    return Edge(
        subject=("poll", poll_id),
        predicate="elevated_in",
        object=("domain", domain),
        weight=6.0,
        as_of=poll_id,
    )


def _seed_graph(path: Path, per_poll_elevated: list[list[str]]) -> None:
    """Seed ``path`` with one poll per entry in ``per_poll_elevated``; each entry lists the
    domains elevated in that poll. Polls are ordered chronologically by their string id."""
    edges: list[Edge] = []
    for i, doms in enumerate(per_poll_elevated):
        poll_id = f"2026-01-01T{i:02d}:00:00+00:00"
        for d in doms:
            edges.append(_elevated_edge(poll_id, d))
    append_edges(edges, path=path)


# ---- classifier map coverage for the two new classes ---------------------------------------


def test_class_to_domains_covers_every_overlay_class() -> None:
    """Every overlay class the classifier can return must have a domain list — otherwise the
    persistence gate silently no-ops on that class."""
    from trading_live_claude.intel.overlay import OVERLAY_CLASSES
    missing = [c for c in OVERLAY_CLASSES if c not in _CLASS_TO_DOMAINS]
    assert not missing, f"_CLASS_TO_DOMAINS missing: {missing}"


# ---- gate: happy path ----------------------------------------------------------------------


def test_persistence_gate_allows_entry_when_no_domain_has_persisted(tmp_path: Path) -> None:
    """No elevated_in edges at all — every symbol is clear regardless of class."""
    path = tmp_path / "intel_graph.jsonl"
    _seed_graph(path, [[], [], [], [], [], []])         # 6 empty polls
    gate = PersistenceGate(min_polls=5, refresh_seconds=0.0, graph_path=path)
    halt, reason = gate("AAPL")
    assert halt is False and reason == ""


def test_persistence_gate_halts_when_a_relevant_domain_has_persisted_long_enough(
    tmp_path: Path,
) -> None:
    """Equity is exposed to 'conflict' + 'military'. Seed 6 consecutive polls with 'conflict'
    elevated → equity entries must halt at min_polls=5, and the reason names the driver."""
    path = tmp_path / "intel_graph.jsonl"
    _seed_graph(path, [["conflict"]] * 6)
    gate = PersistenceGate(min_polls=5, refresh_seconds=0.0, graph_path=path)
    halt, reason = gate("AAPL")
    assert halt is True
    assert "conflict" in reason and "equity" in reason
    assert "6" in reason           # cites the actual run length, not just the threshold


def test_persistence_gate_ignores_domains_the_class_is_not_exposed_to(tmp_path: Path) -> None:
    """Crypto is exposed only to conflict + military per _CLASS_TO_DOMAINS. Elevating 'economy'
    for 20 polls must NOT halt a crypto entry."""
    path = tmp_path / "intel_graph.jsonl"
    _seed_graph(path, [["economy"]] * 20)
    gate = PersistenceGate(min_polls=5, refresh_seconds=0.0, graph_path=path)
    halt, _ = gate("BTC/USD")
    assert halt is False


def test_persistence_gate_reports_the_longest_running_domain_when_multiple_qualify(
    tmp_path: Path,
) -> None:
    """When two exposed domains cross the threshold, the reason should name the longer run."""
    path = tmp_path / "intel_graph.jsonl"
    # 8 polls of conflict, 6 of military — commodity is exposed to both (via 'conflict', not
    # 'military' actually, so use future which is exposed to both + energy).
    # Simpler: use ('conflict','military') both for future class.
    seeds = [["conflict", "military"]] * 6 + [["conflict"]] * 2
    _seed_graph(path, seeds)
    gate = PersistenceGate(min_polls=5, refresh_seconds=0.0, graph_path=path)
    halt, reason = gate("/ES")            # future
    assert halt is True
    assert "conflict" in reason           # conflict has the 8-run vs military's 6-run break


# ---- gate: robustness ----------------------------------------------------------------------


def test_persistence_gate_fails_open_when_the_graph_file_is_missing(tmp_path: Path) -> None:
    """A monitor started before the graph exists must not spuriously halt every entry —
    fail-open is the whole point of the log.warning + empty-dict path."""
    gate = PersistenceGate(min_polls=5, refresh_seconds=0.0,
                            graph_path=tmp_path / "does_not_exist.jsonl")
    halt, _ = gate("AAPL")
    assert halt is False


def test_persistence_gate_fails_open_on_corrupt_graph_lines(tmp_path: Path) -> None:
    """A partial write / garbage line in the JSONL must not raise past the gate."""
    path = tmp_path / "intel_graph.jsonl"
    path.write_text("not-json-at-all\n{\"partial\":\"junk\"\n", encoding="utf-8")
    gate = PersistenceGate(min_polls=5, refresh_seconds=0.0, graph_path=path)
    halt, _ = gate("AAPL")
    # load_edges is lenient — it either parses what it can or returns []; either way the gate
    # must not halt on graph parse trouble.
    assert halt is False


def test_persistence_gate_respects_the_refresh_interval(tmp_path: Path) -> None:
    """Between refreshes the gate must reuse its cached decision — otherwise a tight monitor
    loop would re-read the whole graph file every 60 seconds."""
    path = tmp_path / "intel_graph.jsonl"
    _seed_graph(path, [[]])                 # one empty poll
    gate = PersistenceGate(min_polls=5, refresh_seconds=3600.0, graph_path=path)
    halt_a, _ = gate("AAPL")
    # Now overwrite the graph so a fresh read WOULD halt.
    path.unlink()
    _seed_graph(path, [["conflict"]] * 10)
    halt_b, _ = gate("AAPL")
    # Same read — should still be the cached (empty) decision, not a fresh halt.
    assert halt_a == halt_b == False
    # Force a refresh: now it must halt.
    gate.refresh(force=True)
    halt_c, _ = gate("AAPL")
    assert halt_c is True


def test_persistence_gate_uses_overrides_for_class_resolution(tmp_path: Path) -> None:
    """A caller can pin a symbol to a class explicitly (mirrors OverlayProvider's overrides)."""
    path = tmp_path / "intel_graph.jsonl"
    _seed_graph(path, [["economy"]] * 10)
    # By default AAPL is equity — not exposed to economy → allowed.
    gate = PersistenceGate(min_polls=5, refresh_seconds=0.0, graph_path=path)
    assert gate("AAPL")[0] is False
    # Override to fixed_income (which IS exposed to economy) → halt.
    gate2 = PersistenceGate(min_polls=5, refresh_seconds=0.0, graph_path=path,
                              class_overrides={"AAPL": "fixed_income"})
    assert gate2("AAPL")[0] is True


# ---- classifier: new class regression -------------------------------------------------------


def test_classifier_routes_bonds_to_fixed_income() -> None:
    assert classify_symbol("TLT") == "fixed_income"
    assert classify_symbol("ZAG.TO") == "fixed_income"


def test_classifier_routes_metals_to_precious_metals_not_commodity() -> None:
    assert classify_symbol("GLD") == "precious_metals"
    assert classify_symbol("CGL.TO") == "precious_metals"
    assert classify_symbol("USO") == "commodity"                 # oil ETF still commodity
