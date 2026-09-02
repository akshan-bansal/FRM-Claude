"""Inspect the current shape of ``state/intel_graph.jsonl`` without fetching anything.

Companion to ``scripts/graph_journal.py``: that one grows the journal from live snapshots; this
one just reports what is already there — vertex counts by type, edge counts by predicate,
distinct sources, top-N events by corroboration count, and one-line samples per predicate. Cheap
to run repeatedly during the iterative vertex/edge development posture (NEXT_SESSION.md #6).

Output goes to stdout AND to ``reports/graph_profile.md`` so the state is easy to diff over time.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path

from trading_live_claude.intel.graph import (
    DEFAULT_GRAPH_JOURNAL,
    Edge,
    edges_where,
    load_edges,
)


REPORT = Path("reports/graph_profile.md")


def _corroboration_map(edges: list[Edge]) -> dict[str, set[str]]:
    """Event id → set of distinct source names (from ``mentioned_by`` edges)."""
    out: dict[str, set[str]] = defaultdict(set)
    for e in edges_where(edges, predicate="mentioned_by"):
        if e.subject[0] == "event":
            out[e.subject[1]].add(e.object[1])
    return out


def _lines(edges: list[Edge]) -> list[str]:
    if not edges:
        return ["_Journal is empty. Run scripts/graph_journal.py to accrete edges._"]

    per_pred: Counter[str] = Counter(e.predicate for e in edges)
    nodes: set[tuple[str, str]] = set()
    for e in edges:
        nodes.add(e.subject)
        nodes.add(e.object)
    by_type: Counter[str] = Counter(n[0] for n in nodes)

    src_counts: Counter[str] = Counter()
    for e in edges_where(edges, predicate="mentioned_by"):
        if e.object[0] == "source":
            src_counts[e.object[1]] += 1

    dom_counts: Counter[str] = Counter()
    for e in edges_where(edges, predicate="about_domain"):
        if e.object[0] == "domain":
            dom_counts[e.object[1]] += 1

    reg_counts: Counter[str] = Counter()
    for e in edges_where(edges, predicate="affects_region"):
        if e.object[0] == "region":
            reg_counts[e.object[1]] += 1

    corr = _corroboration_map(edges)
    top_corr = sorted(corr.items(), key=lambda kv: len(kv[1]), reverse=True)[:10]

    L: list[str] = []
    L.append("# intel graph profile")
    L.append("")
    L.append(f"**Edges total:** {len(edges)}  |  **Nodes total:** {len(nodes)}")
    L.append("")
    L.append("## Nodes by type")
    L.append("")
    L.append("| type | count |")
    L.append("|---|---|")
    for t, c in sorted(by_type.items(), key=lambda kv: -kv[1]):
        L.append(f"| {t} | {c} |")
    L.append("")
    L.append("## Edges by predicate")
    L.append("")
    L.append("| predicate | count |")
    L.append("|---|---|")
    for p, c in sorted(per_pred.items(), key=lambda kv: -kv[1]):
        L.append(f"| {p} | {c} |")
    L.append("")

    if src_counts:
        L.append("## Top sources by ``mentioned_by`` count")
        L.append("")
        L.append("| source | mentions |")
        L.append("|---|---|")
        for s, c in src_counts.most_common(15):
            L.append(f"| {s} | {c} |")
        L.append("")

    if dom_counts:
        L.append("## Top domains by ``about_domain`` count")
        L.append("")
        L.append("| domain | events |")
        L.append("|---|---|")
        for d, c in dom_counts.most_common(15):
            L.append(f"| {d} | {c} |")
        L.append("")

    if reg_counts:
        L.append("## Top regions by ``affects_region`` count")
        L.append("")
        L.append("| region | events |")
        L.append("|---|---|")
        for r, c in reg_counts.most_common(15):
            L.append(f"| {r} | {c} |")
        L.append("")

    if top_corr:
        L.append("## Best-corroborated events (distinct sources per event)")
        L.append("")
        L.append("| event id | source count | sources |")
        L.append("|---|---|---|")
        for ev, sources in top_corr:
            L.append(f"| `{ev}` | {len(sources)} | {', '.join(sorted(sources))} |")
        L.append("")

    L.append("_This report is what the overlay and interpreter can reason over today. "
             "Corroboration is now a first-class graph property (distinct sources per event); "
             "persistence is per-edge across consecutive polls. Neither requires the agent layer._")
    return L


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", type=Path, default=Path(DEFAULT_GRAPH_JOURNAL))
    args = ap.parse_args()

    edges = load_edges(args.journal)
    lines = _lines(edges)
    text = "\n".join(lines)

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n[graph_profile] {len(edges)} edges from {args.journal} -> {REPORT}")


if __name__ == "__main__":
    main()
