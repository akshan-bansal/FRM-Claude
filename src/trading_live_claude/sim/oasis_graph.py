"""Read-only adapter: an OASIS run's SQLite database -> typed graph edges (and a herding measure).

OASIS keeps its agent graph as opaque simulation state: there is no hook to ask it for follows or
engagement, and ``intel/graph.py`` says so. What it DOES leave behind is the database it writes, which
holds every user, post, comment, like, dislike and follow. This module reads that file, never the
simulator, and turns it into journal edges so a simulated society is a connected part of the graph
rather than a number in a result file.

Rules this module keeps:

* **Read-only.** The database is opened with ``mode=ro``; nothing is written to it.
* **Untrusted text.** Posts and comments are model output. They are scanned to count symbol
  mentions and are never stored in an edge, logged, or interpreted. Edges carry ids and counts only,
  so a post that says "ignore your instructions" changes nothing but a count.
* **Counts, not views.** ``attends`` records how many items an agent wrote mentioning a symbol. No
  sentiment is read here; a stance still comes from the model readout in the result file.
* **The seed post is not the agent's.** The runner publishes the desk brief as agent 0 through a
  manual action. Counting it would credit agent 0 with attention to every watched symbol, so the first
  post is skipped when it carries the runner's fixed prefix.
* **Deterministic.** Same database, same edges, same order.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ..intel.graph import Edge, NodeType
from .society import MIN_AGENTS_FOR_SPECTRUM, top_eigenvalue_share

# The runner's desk-brief post always starts with this (vm/oasis_runner.py); see the module docstring.
SEED_PREFIX = "Desk brief as of"

# Engagement kinds recorded on ``engaged`` edges, in the order they appear in ``meta``.
_KINDS = ("likes", "dislikes", "comments", "reposts")


@dataclass(frozen=True)
class OasisRun:
    """What one run's database says, reduced to ids and text to be counted."""

    agents: tuple[int, ...]                        # agent ids, sorted
    items: tuple[tuple[int, str], ...]             # (author agent, text) for posts and comments
    follows: tuple[tuple[int, int], ...]           # (follower agent, followee agent)
    engagement: tuple[tuple[int, int, str], ...]   # (actor agent, author agent, kind)
    seed_posts_skipped: int


def read_oasis_db(path: Path, *, seed_prefix: str = SEED_PREFIX) -> OasisRun:
    """Load a run. Raises ``FileNotFoundError`` / ``ValueError`` rather than returning a guess.

    Tables a given OASIS version does not have (``dislike``, ``follow``, ...) are treated as empty.
    """
    if not path.exists():
        raise FileNotFoundError(path)
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {str(r[0]) for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"user", "post"} <= tables:
            raise ValueError(f"{path} is not an OASIS database (needs the user and post tables)")

        def rows(table: str, sql: str) -> list[tuple[Any, ...]]:
            return [tuple(r) for r in con.execute(sql)] if table in tables else []

        agent_of = {int(u): int(a) for u, a in
                    con.execute('SELECT user_id, COALESCE(agent_id, user_id) FROM "user"')}
        agents = tuple(sorted(set(agent_of.values())))

        author_of_post: dict[int, int] = {}
        items: list[tuple[int, str]] = []
        engagement: list[tuple[int, int, str]] = []
        reposts: list[tuple[int, int]] = []            # (author, ORIGINAL post id), resolved below
        skipped = 0
        post_sql = ("SELECT post_id, user_id, original_post_id, content, quote_content "
                    "FROM post ORDER BY post_id")
        for i, (pid, uid, orig, content, quote) in enumerate(rows("post", post_sql)):
            author = agent_of.get(int(uid)) if uid is not None else None
            if author is None:
                continue
            author_of_post[int(pid)] = author
            if i == 0 and isinstance(content, str) and content.startswith(seed_prefix):
                skipped += 1                           # the runner's brief, not this agent's writing
                continue
            if orig is not None:
                # A repost or quote: engagement with the original's author; only a quote adds text.
                reposts.append((author, int(orig)))
                if isinstance(quote, str) and quote.strip():
                    items.append((author, quote))
            elif isinstance(content, str):
                items.append((author, content))
        for author, orig_post in reposts:
            target = author_of_post.get(orig_post)
            if target is not None:
                engagement.append((author, target, "reposts"))

        for table, kind in (("like", "likes"), ("dislike", "dislikes")):
            for uid, pid in rows(table, f'SELECT user_id, post_id FROM "{table}"'):
                actor, target = agent_of.get(int(uid)), author_of_post.get(int(pid))
                if actor is not None and target is not None:
                    engagement.append((actor, target, kind))
        for pid, uid, content in rows("comment", "SELECT post_id, user_id, content FROM comment"):
            actor, target = agent_of.get(int(uid)), author_of_post.get(int(pid))
            if actor is not None and target is not None:
                engagement.append((actor, target, "comments"))
                if isinstance(content, str):
                    items.append((actor, content))
        engagement = [(a, t, k) for a, t, k in engagement if a != t]           # no self-engagement

        follows = {
            (agent_of[int(a)], agent_of[int(b)])
            for a, b in rows("follow", "SELECT follower_id, followee_id FROM follow")
            if int(a) in agent_of and int(b) in agent_of and agent_of[int(a)] != agent_of[int(b)]
        }
        return OasisRun(agents=agents, items=tuple(items), follows=tuple(sorted(follows)),
                        engagement=tuple(sorted(engagement)), seed_posts_skipped=skipped)
    finally:
        con.close()


def _patterns(symbol: str, aliases: Sequence[str]) -> list[re.Pattern[str]]:
    """How a symbol is recognised in text: the exact pair, the bare ticker, and any given aliases.

    The bare ticker ("BTC" for ``BTC/USD``) matches as a whole word, optionally with a ``$``. Tickers of
    three letters or fewer must be upper-case, so a ticker that is also a word (``ON``) is not
    matched in ordinary prose. Alias words are matched case-insensitively.
    """
    base = symbol.split("/")[0].split(".")[0]
    pats: list[re.Pattern[str]] = []
    if symbol != base:
        # A real pair or suffixed ticker (BTC/USD, XIC.TO) is specific enough to match in any case,
        # but still only as a whole token: an unbounded match is how "ON" ends up inside "carry on".
        pats.append(re.compile(rf"(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9])", re.IGNORECASE))
    pats.append(re.compile(rf"(?<![A-Za-z0-9])\$?{re.escape(base)}(?![A-Za-z0-9])",
                           0 if len(base) <= 3 else re.IGNORECASE))
    pats += [re.compile(rf"(?<![A-Za-z0-9]){re.escape(a)}(?![A-Za-z0-9])", re.IGNORECASE)
             for a in aliases]
    return pats


def _mention_counts(run: OasisRun, symbols: Sequence[str],
                    aliases: Mapping[str, Sequence[str]]) -> dict[tuple[int, str], int]:
    """(agent, symbol) -> number of that agent's items mentioning the symbol (presence per item)."""
    pats = {s: _patterns(s, aliases.get(s, ())) for s in symbols}
    counts: Counter[tuple[int, str]] = Counter()
    for author, text in run.items:
        for s, ps in pats.items():
            if any(p.search(text) for p in ps):
                counts[(author, s)] += 1
    return dict(counts)


def oasis_edges(run: OasisRun, *, run_id: str, as_of: str, symbols: Sequence[str],
                aliases: Mapping[str, Sequence[str]] | None = None) -> list[Edge]:
    """Typed edges for one run, stamped with the SEED's ``as_of`` (never the wall clock).

    ``member_of`` for every agent (so a silent agent is still on the record), ``follows``,
    aggregated ``engaged`` (counts per ordered pair, with the breakdown in ``meta``), and ``attends``
    (items mentioning each symbol). No edge carries text, and none carries an influence.
    """
    soc: tuple[NodeType, str] = ("society", run_id)

    def agent(a: int) -> tuple[NodeType, str]:
        return ("agent", f"{run_id}:{a}")

    out: list[Edge] = []
    for a in run.agents:
        out.append(Edge(agent(a), "member_of", soc, weight=1.0, as_of=as_of,
                        meta={"agent_id": float(a), "run_id": run_id}))
    for f, t in run.follows:
        out.append(Edge(agent(f), "follows", agent(t), weight=1.0, as_of=as_of,
                        meta={"run_id": run_id}))
    pair_kinds: dict[tuple[int, int], Counter[str]] = {}
    for actor, target, kind in run.engagement:
        pair_kinds.setdefault((actor, target), Counter())[kind] += 1
    for (actor, target), kinds in sorted(pair_kinds.items()):
        meta: dict[str, float | str] = {k: float(kinds.get(k, 0)) for k in _KINDS}
        meta["run_id"] = run_id
        out.append(Edge(agent(actor), "engaged", agent(target), weight=float(sum(kinds.values())),
                        as_of=as_of, meta=meta))
    for (a, s), n in sorted(_mention_counts(run, symbols, aliases or {}).items()):
        out.append(Edge(agent(a), "attends", ("symbol", s), weight=float(n), as_of=as_of,
                        meta={"items": float(n), "run_id": run_id}))
    return out


def attention_matrix(run: OasisRun, symbols: Sequence[str],
                     aliases: Mapping[str, Sequence[str]] | None = None) -> np.ndarray:
    """Agents x symbols matrix of mention counts (rows follow ``run.agents``, columns ``symbols``)."""
    counts = _mention_counts(run, symbols, aliases or {})
    row = {a: i for i, a in enumerate(run.agents)}
    col = {s: j for j, s in enumerate(symbols)}
    m = np.zeros((len(run.agents), len(symbols)), dtype=float)
    for (a, s), n in counts.items():
        m[row[a], col[s]] = n
    return m


def herd_from_run(run: OasisRun, symbols: Sequence[str],
                  aliases: Mapping[str, Sequence[str]] | None = None, *,
                  min_agents: int = MIN_AGENTS_FOR_SPECTRUM) -> float | None:
    """Attention concentration: top-eigenvalue share of the agents' symbol-attention correlation.

    This is the ``herd`` input of ``society_influence``, available without a per-agent stance matrix:
    do the agents attend to the same symbols together? ``None`` when the run is too small to say
    (fewer than ``min_agents`` agents, or fewer than two symbols anyone varied on), which the adapter
    treats as "term absent", not as zero.
    """
    return top_eigenvalue_share(attention_matrix(run, symbols, aliases), min_agents=min_agents)
