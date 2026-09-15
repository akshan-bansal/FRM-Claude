from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from trading_live_claude.futures import overlay_class_for
from trading_live_claude.intel.graph import Edge, append_edges
from trading_live_claude.intel.graph_interpret import (
    GraphInterpreter,
    domain_persistence,
    weigh_by_graph,
)
from trading_live_claude.intel.interpret import THEME_EXEMPLARS, Thesis, implicated_symbols
from trading_live_claude.intel.routing import PersistenceGate


def _polls(domain: str, elevated: list[bool]) -> list[Edge]:
    edges: list[Edge] = []
    for i, on in enumerate(elevated):
        poll = ("poll", f"2026-09-13T0{i}:00:00+00:00")
        edges.append(Edge(poll, "observed", ("domain", "economy"), weight=1.0, as_of=poll[1]))
        if on:
            edges.append(Edge(poll, "elevated_in", ("domain", domain), weight=2.0, as_of=poll[1]))
    return edges


def _thesis(confidence: str, themes: list[str], name: str = "Energy supply shock") -> Thesis:
    return Thesis(name=name, confidence=confidence, evidence=["energy_stress 0.8"], inference="i",
                  action="a", themes=themes)


def test_persistent_domain_promotes_and_a_quiet_latest_poll_demotes() -> None:
    persistent = domain_persistence(_polls("energy", [True, True, True, True]), ["energy"])
    assert persistent == {"energy": 4}
    (up,) = weigh_by_graph([_thesis("moderate", ["energy"])], persistent, min_polls=3)
    assert up.confidence == "high" and "elevated 4 consecutive polls" in up.evidence[-1]

    quiet = domain_persistence(_polls("energy", [True, True, False]), ["energy"])
    (down,) = weigh_by_graph([_thesis("moderate", ["energy"])], quiet, min_polls=3)
    assert down.confidence == "tentative"

    brief = domain_persistence(_polls("energy", [False, True, True]), ["energy"])
    (same,) = weigh_by_graph([_thesis("moderate", ["energy"])], brief, min_polls=3)
    assert same.confidence == "moderate"


def test_confidence_stays_on_the_ladder_and_null_thesis_is_untouched() -> None:
    (top,) = weigh_by_graph([_thesis("high", ["energy"])], {"energy": 9}, min_polls=3)
    (bottom,) = weigh_by_graph([_thesis("tentative", ["energy"])], {"energy": 0}, min_polls=3)
    assert (top.confidence, bottom.confidence) == ("high", "tentative")
    null = _thesis("tentative", ["energy"], name="No notable configuration")
    assert weigh_by_graph([null], {"energy": 0}, min_polls=3) == [null]


def test_graph_interpreter_reads_the_journal_and_leaves_theses_alone_when_unreadable(tmp_path: Path,
                                                                                   monkeypatch) -> None:
    from trading_live_claude.intel import graph_interpret

    monkeypatch.setattr(graph_interpret, "interpret", lambda snap: [_thesis("moderate", ["energy"])])
    journal = tmp_path / "graph.jsonl"
    append_edges(_polls("energy", [True, True, True]), path=journal)
    weighted = GraphInterpreter(lambda: object(), min_polls=3, graph_path=journal)()
    assert weighted[0].confidence == "high"

    missing = GraphInterpreter(lambda: object(), graph_path=tmp_path / "nope" / "x.jsonl")()
    assert missing[0].confidence == "moderate"                  # no graph = no evidence, not quiet
    assert GraphInterpreter(lambda: None, graph_path=journal)() == []


def test_futures_are_exemplars_so_theses_implicate_them() -> None:
    assert "/MCL" in THEME_EXEMPLARS["energy"] and "/MGC" in THEME_EXEMPLARS["safe_haven"]
    implicated = implicated_symbols([_thesis("high", ["energy", "materials"])])
    assert "/NG" in implicated["energy"] and "/FEF" in implicated["materials"]


def test_futures_overlay_classes_and_persistence_gate_route_them() -> None:
    gold = SimpleNamespace(long_name="E-Micro Gold")
    crude = SimpleNamespace(long_name="Micro WTI Crude Oil")
    assert overlay_class_for(gold) == "precious_metals"
    assert overlay_class_for(crude) == "commodity"

    gate = PersistenceGate(min_polls=3, class_overrides={"/MCL": "commodity"})
    gate._persistence_by_domain = {"energy": 4}
    gate._ts = float("inf")
    halt, reason = gate("/MCL")
    assert halt and "commodity" in reason and "energy" in reason
