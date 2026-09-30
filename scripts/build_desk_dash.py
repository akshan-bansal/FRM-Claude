"""Rebuild ``pwa/desk.html`` from the live journals — the desk panel's data is baked in at build time.

Why baked in rather than fetched: the panel is a static page served to an 8" touch device on the LAN,
with no backend of its own. Fetching would mean standing up an endpoint that exposes the whole
journal; instead this script reads ``state/`` directly, computes every figure the panel draws, and
injects one JSON blob into ``pwa/desk.template.html``. Re-run it whenever the numbers should catch up
("repopulate the dash"). It reads state/ plus the meter catalogue and the calibration report, and
writes only its ``--out`` pages — by default ``pwa/desk.html`` and the copy in
``OS-InvestmentIntelligenceDashboard/``, byte-identical, so the two never drift.

**Data that cannot be represented faithfully is excluded, not annotated** (user principle,
2026-09-24). A session killed with positions still open reports ``realized_pnl = 0.00`` for ever,
because ``PaperBroker._apply_fill`` only accrued realised P&L on a full close, and its last marks sit
frozen against stale prices. 48 of the 80 sessions in this journal are in that state. Such a session
does not appear in an aggregate with a caution chip beside it — it is filtered out, and the scope of
what remains is stated instead (``data.scope``). The same rule drops sessions with too few marks to
compute a ratio, and sessions whose return is so small the ratio is a denominator artefact.

Other exclusions of the same kind: EXPIRED prompts are kept out of card response time, because their
``resolved_at`` is when the sweep noticed them and not a decision; and positions still open are kept
out of every P&L figure, with the count reported so the omission is visible.

    python scripts/build_desk_dash.py
    python scripts/build_desk_dash.py --state-dir state --out pwa/desk.html
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import csv
import json
import math
import sqlite3
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")    # type: ignore[union-attr]
    except Exception:                                                  # pragma: no cover
        pass

MIN_MARKS = 25          # a session needs this many equity marks before a ratio means anything
TRADING_DAYS = 365      # the desk's crypto sleeve trades every day; ratios annualise on 365
NOISE_FLOOR = 0.0005    # |return| under 5bp: sortino/dd is a denominator artefact, so the session is
                        # not fit for the ranking and is excluded from it
FLAT_EPS = 0.01         # positions_value at or below this counts as a closed book


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def session_metrics(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Sortino, Sharpe, max drawdown and sortino_over_dd for one session's equity marks."""
    eq = [float(r["equity"]) for r in rows]
    rets = [(eq[i] - eq[i - 1]) / eq[i - 1] for i in range(1, len(eq)) if eq[i - 1]]
    down = [r for r in rets if r < 0]
    peak, dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak)
    mean = statistics.fmean(rets) if rets else 0.0
    dstd = (statistics.pstdev(down) if len(down) > 1 else 0.0) or 1e-9
    sstd = (statistics.pstdev(rets) if len(rets) > 1 else 0.0) or 1e-9
    sortino = mean / dstd * math.sqrt(TRADING_DAYS)
    return {
        "rows": len(rows), "start": rows[0]["ts"][:16], "eq0": eq[0], "eq1": eq[-1],
        "ret": eq[-1] / eq[0] - 1, "maxdd": dd, "sortino": sortino,
        "sharpe": mean / sstd * math.sqrt(TRADING_DAYS),
        "sod": (sortino / dd) if dd else 0.0,
        "realized": float(rows[-1]["realized_pnl"]),
    }


def session_fitness(marks: list[dict[str, str]]) -> tuple[bool, str]:
    """``(fit, reason_if_not)`` for one session's equity marks.

    A session that never closed flat is the important case: its ``realized_pnl`` column is 0.00 by
    construction and its final marks are stale, so nothing derived from it can be represented
    faithfully. It is excluded from every aggregate rather than shown with a warning.
    """
    if abs(float(marks[-1]["positions_value"])) > FLAT_EPS:
        return False, "never closed flat"
    if len(marks) < MIN_MARKS:
        return False, f"under {MIN_MARKS} marks"
    if abs(session_metrics(marks)["ret"]) < NOISE_FLOOR:
        return False, "return under 5bp"
    return True, ""


def histogram(values: list[float], bins: int) -> dict[str, Any]:
    lo, hi = min(values), max(values)
    width = (hi - lo) / bins or 1.0
    counts = [0] * bins
    for v in values:
        counts[min(bins - 1, int((v - lo) / width))] += 1
    return {"lo": lo, "hi": hi, "w": width, "bins": counts}


# --- the 120-meter evidence atlas ---------------------------------------------------------- #
# The atlas panel was a separate page; it is folded into the desk panel as a fourth tab. Its
# catalogue is a statement of what the code *defines*, not of what has been observed, so the
# value/status columns of the CSV (uniformly "NO DATA") are dropped here: a reading only ever
# reaches the page from a snapshot, carrying its own source, scope and timestamp.
CATALOG_FIELDS = ("id", "group", "label", "source", "formula", "unit", "kind", "support", "url")

# Six meters the committed calibration report can answer. The report is a faithful record of a
# real sweep, so it is fit to display — what it lacks is an observation timestamp and a claim to
# be the *chosen* configuration, and both of those go in the snapshot's stated scope rather than
# into a caution chip on a number. Signs are normalised to each meter's definition: meter 009 is
# a positive loss magnitude, while the report writes drawdown negative.
CALIBRATION_METERS = (
    ("004", "oos_return", "%", "pct"),
    ("009", "oos_maxdd", "%", "magnitude_pct"),
    ("071", "oos_trades", "trades", "raw"),
    ("075", "oos_win_rate", "%", "pct"),
    ("079", "oos_score", "score", "raw"),
    ("080", "wfe", "ratio", "raw"),
)


def read_meter_catalog(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    with path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            bounds = None
            if (raw := (r.get("bounds") or "").strip()):
                try:
                    lo, hi = json.loads(raw)
                    bounds = [float(lo), float(hi)]
                except (ValueError, TypeError):
                    bounds = None         # an unparseable scale is no scale, not a guessed one
            row = {k: (r.get(k) or "") for k in CATALOG_FIELDS}
            row["bounds"] = bounds
            out.append(row)
    out.sort(key=lambda r: r["id"])
    return out


def calibration_snapshot(path: Path, catalog_ids: set[str]) -> dict[str, Any] | None:
    """The first row of the calibration sweep, as a selectable historical snapshot.

    All six readings or none: a partial row would put an arbitrary subset of a research
    configuration on screen, and the row is only meaningful as a whole.
    """
    if not path.exists():
        return None
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return None
    row = rows[0]
    scope = " · ".join(p for p in (
        row.get("sym"), row.get("strategy"),
        f"{row.get('sweep_param')}={row.get('sweep_value')}" if row.get("sweep_param") else "",
        row.get("fixed_kwargs"), row.get("asset_class"),
        f"{row.get('folds')} folds" if row.get("folds") else "") if p)
    readings: dict[str, Any] = {}
    for meter, column, unit, transform in CALIBRATION_METERS:
        if meter not in catalog_ids or column not in row:
            return None
        try:
            raw = float(row[column])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(raw):
            return None
        if transform == "pct":
            value = 100.0 * raw
        elif transform == "magnitude_pct":
            value = 100.0 * abs(raw)
        else:
            value = raw
        if unit == "%" and not 0.0 <= value <= 100.0 and meter == "009":
            return None                   # a drawdown outside [0, 100] cannot be drawn on its scale
        readings[meter] = {
            "value": value, "unit": unit, "as_of": None, "status": "HISTORICAL",
            "source": f"{path.as_posix()}: first data row / {column}",
            "scope": scope,
            "reason": ("Committed report; the source carries no observation timestamp. "
                       "Historical out-of-sample research, not current portfolio state."),
            "kind": "number",
        }
    return {
        "label": f"HISTORICAL · {row.get('sym', '?')} calibration row",
        "notice": (f"HISTORICAL RESEARCH · {scope}. Six values read from the first data row of "
                   f"{path.as_posix()} — the first row, not a selected winner, and not current "
                   f"trading results. The report carries no observation timestamp, so these "
                   f"readings are dated only by the file."),
        "readings": readings,
    }

def catalog_provenance(meters: list[dict[str, Any]]) -> dict[str, str]:
    """Where the catalogue was read from, resolved through git rather than asserted.

    The atlas is a review of what the code *defines* at one commit. That commit is already in every
    row's pinned URL, so it is recovered from there and dated from the repository; if the rows
    disagree, the catalogue has no single provenance and says so instead of naming one.
    """
    shas = {m["url"].split("/blob/", 1)[1].split("/", 1)[0]
            for m in meters if "/blob/" in (m.get("url") or "")}
    if len(shas) != 1:
        return {}
    sha = shas.pop()
    out = {"sha": sha[:7]}
    try:
        got = subprocess.run(["git", "log", "-1", "--format=%h|%cs", sha],
                             capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        short, date = got.split("|", 1)
        out = {"sha": short, "date": date}
        heads = subprocess.run(["git", "branch", "--contains", sha, "--format=%(refname:short)"],
                               capture_output=True, text=True, timeout=10, check=True).stdout.split()
        if heads:
            out["branch"] = heads[0]
    except (OSError, subprocess.SubprocessError, ValueError):
        pass                              # an unresolvable commit is still a commit id, just undated
    return out

def last_prompt(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The most recent prompt that actually reached a card, for the panel's replay view.

    The panel used to open a specimen prompt with invented figures. This is the real record
    instead: one row of ``intents``, shown with the verdict it was actually given. Its fingerprint
    is derived from the stored canonical bytes by the same function the shim and the card use, so
    the three abbreviations can be compared. A row without canonical bytes cannot be fingerprinted
    and is not offered.
    """
    try:
        from trading_live_claude.execution.approval import fingerprint
    except ImportError:
        return None
    decided = [r for r in rows if r.get("canonical") and r.get("issued_at")]
    if not decided:
        return None
    r = max(decided, key=lambda x: str(x["issued_at"]))
    took: float | None = None
    ttl: int | None = None
    try:
        issued = datetime.fromisoformat(str(r["issued_at"]))
        if r.get("resolved_at") and str(r["verdict"]) in ("ACCEPT", "DECLINE"):
            took = round((datetime.fromisoformat(str(r["resolved_at"])) - issued).total_seconds(), 1)
        if r.get("expires_at"):
            ttl = int((datetime.fromisoformat(str(r["expires_at"])) - issued).total_seconds())
    except ValueError:
        pass
    return {
        "symbol": r["symbol"], "action": r["action"], "shares": r["shares"],
        "entry": r["entry"], "stop": r["stop"], "notional_usd": r["notional_usd"],
        "risk_dollars": r["risk_dollars"], "strategy": r["strategy"], "account": r["account"],
        "mode": r["mode"], "broker": r["broker"], "thesis": r.get("thesis") or "",
        "fingerprint": fingerprint(str(r["canonical"]).encode("utf-8")),
        "verdict": str(r["verdict"] or "UNRESOLVED"), "took_s": took, "ttl_granted": ttl,
        "signed": bool(r.get("signature")), "issued_at": str(r["issued_at"]),
    }


def build(state: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}

    # ---- equity: per-session metrics, the richest curve, the ranking ---------------------- #
    rows = list(csv.DictReader((state / "paper_equity.csv").open(encoding="utf-8")))
    sessions: dict[str, list[dict[str, str]]] = collections.defaultdict(list)
    for r in rows:
        sessions[r["session_id"]].append(r)
    ranked: list[dict[str, Any]] = []
    excluded: collections.Counter[str] = collections.Counter()
    fit_sessions: dict[str, list[dict[str, str]]] = {}
    for sid, marks in sessions.items():
        fit, reason = session_fitness(marks)
        if not fit:
            excluded[reason] += 1
            continue
        fit_sessions[sid] = marks
        m = session_metrics(marks)
        m["sid"] = sid[:8]
        ranked.append(m)
    ranked.sort(key=lambda m: -m["sod"])
    data["sessions"] = ranked[:9]
    data["ranked_count"] = len(ranked)
    data["session_count"] = len(sessions)
    data["plane"] = [{"sid": m["sid"], "dd": m["maxdd"], "sortino": m["sortino"],
                      "rows": m["rows"], "ret": m["ret"], "sod": m["sod"]} for m in ranked[:9]]
    data["scope"] = {
        "sessions_seen": len(sessions),
        "sessions_used": len(ranked),
        "excluded": sorted(excluded.items(), key=lambda kv: -kv[1]),
        "rule": ("Aggregates cover sessions that closed flat with enough marks to compute a ratio. "
                 "A session killed with positions open reports realised P&L of 0.00 for ever and "
                 "marks frozen at stale prices, so it is excluded rather than shown with a warning."),
    }
    if not fit_sessions:
        raise SystemExit("[dash] no session is fit to aggregate — nothing to build.")

    richest = max(fit_sessions.values(), key=len)
    curve = [(r["ts"][11:16], round(float(r["equity"]), 2), round(float(r["drawdown_pct"]), 6))
             for r in richest]
    step = max(1, len(curve) // 120)
    data["curve"] = {"sid": richest[0]["session_id"][:8], "points": curve[::step]}
    data["curve_metrics"] = session_metrics(richest)

    eq = [float(r["equity"]) for r in richest]
    bp = [(eq[i] - eq[i - 1]) / eq[i - 1] * 1e4 for i in range(1, len(eq)) if eq[i - 1]]
    data["ret_hist"] = {**histogram(bp, 15), "n": len(bp), "mean": statistics.fmean(bp),
                        "p05": sorted(bp)[max(0, int(len(bp) * 0.05))],
                        "downside": sum(1 for v in bp if v < 0)}

    # ---- fills: attribution, round trips, venue mix, activity ---------------------------- #
    fills = sorted(read_jsonl(state / "paper_fills.jsonl"), key=lambda r: str(r.get("fill_time", "")))
    data["fill_count"] = len(fills)
    book: dict[str, dict[str, Any]] = {}
    per_symbol: collections.Counter[str] = collections.Counter()
    fills_per_symbol: collections.Counter[str] = collections.Counter()
    trades: list[dict[str, Any]] = []
    cells: collections.Counter[tuple[int, int]] = collections.Counter()
    venues: collections.Counter[str] = collections.Counter()
    notional: dict[str, float] = collections.defaultdict(float)

    for f in fills:
        sym, qty, px = f["symbol"], float(f["quantity"]), float(f["price"])
        stamp = str(f.get("fill_time", ""))[:19]
        signed = qty if str(f.get("side")) == "Buy" else -qty
        fills_per_symbol[sym] += 1
        venues[str(f.get("venue") or "?")] += 1
        notional[str(f.get("venue") or "?")] += qty * px
        try:
            when = datetime.fromisoformat(stamp)
            cells[(when.weekday(), when.hour)] += 1
        except ValueError:
            pass

        pos = book.get(sym)
        if pos is None:
            book[sym] = {"qty": signed, "avg": px, "open": stamp, "pnl": 0.0, "fills": 1}
            continue
        pos["fills"] += 1
        if (pos["qty"] > 0) == (signed > 0):                      # adding
            new = pos["qty"] + signed
            pos["avg"] = (pos["avg"] * pos["qty"] + px * signed) / new
            pos["qty"] = new
            continue
        closed = min(abs(pos["qty"]), abs(signed))                # reducing: realise
        realised = (1.0 if pos["qty"] > 0 else -1.0) * (px - pos["avg"]) * closed
        pos["pnl"] += realised
        per_symbol[sym] += realised
        pos["qty"] += signed
        if abs(pos["qty"]) < 1e-12:                               # episode closed
            try:
                mins = (datetime.fromisoformat(stamp)
                        - datetime.fromisoformat(pos["open"])).total_seconds() / 60
            except ValueError:
                mins = 0.0
            trades.append({"sym": sym, "pnl": round(pos["pnl"], 2), "mins": round(mins, 1)})
            del book[sym]

    data["symbols"] = sorted(({"sym": s, "pnl": round(v, 2), "fills": fills_per_symbol[s]}
                              for s, v in per_symbol.items()), key=lambda d: d["pnl"])
    data["venues"] = [{"v": v, "fills": n, "notional": round(notional[v], 0)}
                      for v, n in venues.most_common()]
    data["heat"] = {"cells": [[d, h, n] for (d, h), n in sorted(cells.items())],
                    "max": max(cells.values()) if cells else 0}

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    data["trades"] = {
        "n": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": (len(wins) / len(trades)) if trades else 0.0,
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (gross_loss / len(losses)) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss else 0.0,
        "expectancy": ((gross_win - gross_loss) / len(trades)) if trades else 0.0,
        "median_mins": statistics.median([t["mins"] for t in trades]) if trades else 0.0,
        # Positions still open are in no P&L figure on the panel. Their episodes have not closed, so
        # their P&L is not representable; the count is what makes the omission visible.
        "open_now": len(book),
        "scope": (f"{len(trades)} closed round trips across {len({t['sym'] for t in trades})} "
                  f"symbols; {len(book)} positions still open and excluded"),
    }
    data["trade_hist"] = histogram([t["pnl"] for t in trades], 11) if trades else {
        "lo": 0, "hi": 0, "w": 1, "bins": []}

    # ---- gate denials and per-strategy acceptance ----------------------------------------- #
    reasons: collections.Counter[str] = collections.Counter()
    for r in read_jsonl(state / "rejected.jsonl"):
        for reason in (r.get("reasons") or []):
            reasons[str(reason).split(" below ")[0].split(" >")[0][:46].strip()] += 1
    data["rejections"] = reasons.most_common(7)
    data["rejection_total"] = sum(reasons.values())

    per_strategy: dict[str, dict[str, Any]] = collections.defaultdict(
        lambda: {"n": 0, "acc": 0, "reasons": collections.Counter()})
    for r in read_jsonl(state / "orders.jsonl"):
        s = per_strategy[str(r.get("strategy") or "?")]
        s["n"] += 1
        s["acc"] += 1 if r.get("accepted") else 0
        for reason in (r.get("rejected_reasons") or []):
            s["reasons"][str(reason).split(" below ")[0].split(" >")[0][:34]] += 1
    data["strategies"] = sorted(
        ({"name": k, "n": v["n"], "acc": v["acc"], "rate": v["acc"] / v["n"] if v["n"] else 0.0,
          "top": (v["reasons"].most_common(1)[0][0] if v["reasons"] else "")}
         for k, v in per_strategy.items()), key=lambda d: -d["n"])[:8]

    # ---- header chips: read, not asserted -------------------------------------------------- #
    halted = state / "HALTED"
    data["kill_switch"] = {"halted": halted.exists(),
                           "reason": (halted.read_text(encoding="utf-8").strip()[:80]
                                      if halted.exists() else "")}

    # ---- approvals ------------------------------------------------------------------------ #
    data["approvals"] = approvals(state / "approval.db")

    # ---- intel graph: the MODEL, not the instance ------------------------------------------ #
    data["graph"] = graph_model(state / "intel_graph.jsonl")
    data["oasis"] = oasis_model(state / "intel_agents.jsonl")
    data["oasis"]["sim"] = sim_runs(state / "oasis_runs.jsonl")
    return data


def sim_runs(path: Path) -> list[dict[str, Any]]:
    """Ingested OASIS VM runs (``sim.ingest_result`` validated each against its seed's budget)."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(json.JSONDecodeError, KeyError):
            r = json.loads(line)
            out.append({"run_id": r["result"]["run_id"], "as_of": r["seed"]["as_of"],
                        "withheld": r["seed"]["withheld"], "budget": r["seed"]["budget"],
                        "result": r["result"]})
    return out[-8:][::-1]


def oasis_model(path: Path) -> dict[str, Any]:
    """Summarise the specialist/adversary debate journal (``intel/agents.py::debate``).

    Runs made without an Anthropic key (``api_key_present`` false) carry no claims: every domain
    reads ``no_claim``, which says nothing about the world. They are counted but kept out of the
    outcome tally, and the scope states how many runs the tally covers. No claim -> trade -> P&L
    join exists yet, so nothing here says whether a claim was right.
    """
    out: dict[str, Any] = {"present": path.exists(), "runs": 0, "no_key_runs": 0, "scored_runs": 0,
                           "outcomes": {}, "recent": [], "first": None, "last": None}
    if not path.exists():
        return out
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            r = json.loads(line)
            if isinstance(r, dict) and isinstance(r.get("outcomes"), dict):
                rows.append(r)
    out["runs"] = len(rows)
    if not rows:
        return out
    out["first"], out["last"] = rows[0].get("ts"), rows[-1].get("ts")
    tally: collections.Counter[str] = collections.Counter()
    for r in rows:
        if not r.get("api_key_present"):
            out["no_key_runs"] += 1
            continue
        out["scored_runs"] += 1
        for o in r["outcomes"].values():
            tally[str(o.get("outcome"))] += 1
    out["outcomes"] = dict(tally)
    for r in reversed([x for x in rows if x.get("api_key_present")][-12:]):
        items = []
        for dom, o in r["outcomes"].items():
            claim = o.get("claim") or {}
            crit = o.get("critique") or {}
            fired = o.get("fired") or {}
            items.append({"domain": dom, "outcome": o.get("outcome"),
                          "thesis": claim.get("thesis"), "direction": claim.get("direction"),
                          "claimed": claim.get("confidence"), "final": fired.get("confidence"),
                          "verdict": crit.get("verdict"), "reason": crit.get("reason"),
                          "inference": claim.get("inference"),
                          "evidence": claim.get("evidence") or []})
        out["recent"].append({"ts": r.get("ts"), "as_of": r.get("as_of"),
                              "evidence_items": r.get("evidence_items"),
                              "fired_count": r.get("fired_count"), "items": items})
    return out


def graph_model(path: Path) -> dict[str, Any]:
    """Collapse the intel graph to its schema: node TYPES as vertices, predicates as edges.

    The journal holds ~14k edges over ~640 nodes. Drawing the instance is unreadable at any size
    and tells you nothing you could act on; drawing the MODEL — which kinds of thing exist and how
    they connect — is legible and is what the panel shows. Every count below is read from the
    journal, so the shape is the one the pipeline actually produced, not a diagram of intent.

    **Thickness is edge COUNT, never summed weight.** `weight` is not one quantity: for `traded`
    it is notional dollars (sum ~1.5e6), for `observed` it is a decayed observation score (sum
    ~2e3), and `ranked_by` carries a signed score that sums negative. Putting those on one scale
    would draw a picture whose thickest line means "dollars" and whose thinnest means "score" —
    a number with no referent. Counts are commensurable; weights are reported per-predicate with
    their own unit and never pooled across predicates.
    """
    if not path.exists():
        return {"nodes": [], "links": [], "edges_total": 0, "source": str(path), "present": False}
    node_ids: dict[str, set[str]] = collections.defaultdict(set)
    pair_count: collections.Counter = collections.Counter()
    pair_weight: collections.Counter = collections.Counter()
    total = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                s_t, s_i = e["subject"]
                o_t, o_i = e["object"]
                pred = e["predicate"]
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                continue
            total += 1
            node_ids[s_t].add(str(s_i))
            node_ids[o_t].add(str(o_i))
            key = (s_t, pred, o_t)
            pair_count[key] += 1
            with contextlib.suppress(TypeError, ValueError):
                pair_weight[key] += float(e.get("weight") or 0.0)

    # Influence redistribution: each node's share of the graph's total influence, aggregated to the
    # type the panel draws. Computed through the same node_power() the model uses, so the panel
    # cannot drift from it. Shares, not magnitudes — an unnormalised sum grows without bound as the
    # journal accrues and is not comparable week to week.
    power_by_type: dict[str, float] = collections.defaultdict(float)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from trading_live_claude.intel.graph import Edge as _Edge
        from trading_live_claude.intel.graph import node_power as _node_power
        _edges = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                with contextlib.suppress(Exception):
                    _edges.append(_Edge.from_row(json.loads(line)))
        for (t, _i), v in _node_power(_edges).items():
            power_by_type[t] += v["power"]
    except Exception as e:                                   # pragma: no cover
        print(f"[dash] graph power unavailable: {e}")

    nodes = [{"type": t, "n": len(ids), "power": round(power_by_type.get(t, 0.0), 6)}
             for t, ids in sorted(node_ids.items(), key=lambda kv: -len(kv[1]))]
    links = [{"from": k[0], "pred": k[1], "to": k[2], "n": n,
              "w": round(pair_weight[k], 2)}
             for k, n in sorted(pair_count.items(), key=lambda kv: -kv[1])]
    return {"nodes": nodes, "links": links, "edges_total": total,
            "node_total": sum(len(v) for v in node_ids.values()),
            "source": str(path), "present": True}


def approvals(db: Path) -> dict[str, Any]:
    if not db.exists():
        return {"total": 0, "verdicts": {}, "signed": 0, "unsigned_decided": 0, "cards": [],
                "symbols": [], "decided": [], "median_s": 0, "max_s": 0, "median_use": 0,
                "notional": 0, "expired_rows": 0, "expired_note": "", "last_prompt": None,
                "brokers": {}}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cols = [r[1] for r in con.execute("PRAGMA table_info(intents)")]
    rows = [dict(zip(cols, r, strict=True)) for r in con.execute("SELECT * FROM intents")]
    cards = list(con.execute("SELECT card_id, created_at, revoked_at FROM cards"))
    con.close()

    decided = []
    for r in rows:
        if not (r.get("resolved_at") and r.get("issued_at")):
            continue
        try:
            took = (datetime.fromisoformat(r["resolved_at"])
                    - datetime.fromisoformat(r["issued_at"])).total_seconds()
            ttl = (datetime.fromisoformat(r["expires_at"])
                   - datetime.fromisoformat(r["issued_at"])).total_seconds()
        except ValueError:
            continue
        if str(r["verdict"]) not in ("ACCEPT", "DECLINE"):
            continue                          # see expired_note
        decided.append({"v": str(r["verdict"]), "s": round(took, 2), "ttl": round(ttl, 1),
                        "use": round(took / ttl, 4) if ttl else 0.0,
                        "sym": r["symbol"], "broker": r["broker"]})
    decided.sort(key=lambda d: d["s"])
    signed = sum(1 for r in rows if r.get("signature"))
    return {
        "total": len(rows),
        "verdicts": dict(collections.Counter(str(r["verdict"]) for r in rows)),
        "signed": signed,
        "unsigned_decided": sum(1 for r in rows if r["verdict"] in ("ACCEPT", "DECLINE")) - signed,
        "brokers": dict(collections.Counter(str(r["broker"]) for r in rows)),
        "symbols": collections.Counter(str(r["symbol"]) for r in rows).most_common(6),
        "cards": [{"id": c[0], "created": str(c[1])[:16],
                   "revoked": (str(c[2])[:16] if c[2] else None)} for c in cards],
        "decided": decided,
        "expired_rows": sum(1 for r in rows if str(r["verdict"]) == "EXPIRED"),
        "median_s": round(statistics.median([d["s"] for d in decided]), 2) if decided else 0,
        "max_s": round(max([d["s"] for d in decided]), 2) if decided else 0,
        "median_use": round(statistics.median([d["use"] for d in decided]), 4) if decided else 0,
        "notional": round(sum(float(r["notional_usd"] or 0)
                              for r in rows if r["verdict"] == "ACCEPT"), 0),
        "expired_note": ("EXPIRED rows carry resolved_at from the sweep that noticed them, not a "
                         "card decision, so they are excluded from response time."),
        "last_prompt": last_prompt(rows),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", type=Path, default=Path("state"))
    ap.add_argument("--template", type=Path, default=Path("pwa/desk.template.html"))
    ap.add_argument("--out", type=Path, nargs="+",
                    default=[Path("pwa/desk.html"),
                             Path("OS-InvestmentIntelligenceDashboard/QuantPort.io.html")])
    ap.add_argument("--meters", type=Path,
                    default=Path("OS-InvestmentIntelligenceDashboard/meter_catalog.csv"))
    ap.add_argument("--calibration", type=Path, default=Path("reports/calibration_sweep.csv"))
    args = ap.parse_args()

    if not (args.state_dir / "paper_equity.csv").exists():
        print(f"[dash] no paper_equity.csv under {args.state_dir} — nothing to build from.")
        return 1
    data = build(args.state_dir)
    data["meters"] = read_meter_catalog(args.meters)
    snapshots: dict[str, Any] = {}
    if data["meters"]:
        hist = calibration_snapshot(args.calibration, {m["id"] for m in data["meters"]})
        if hist:
            snapshots["historical"] = hist
    data["meter_snapshots"] = snapshots
    data["meters_pin"] = catalog_provenance(data["meters"]) if data["meters"] else {}
    page = args.template.read_text(encoding="utf-8")
    if "__DATA__" not in page:
        print(f"[dash] {args.template} has no __DATA__ placeholder.")
        return 1
    page = page.replace("__DATA__", json.dumps(data, separators=(",", ":")))
    for out in args.out:
        out.write_text(page, encoding="utf-8")

    a, t = data["approvals"], data["trades"]
    for out in args.out:
        print(f"[dash] {out}  ({out.stat().st_size:,} bytes)")
    if data["meters"]:
        groups = len({m["group"] for m in data["meters"]})
        snaps = ", ".join(data["meter_snapshots"]) or "runtime only"
        print(f"[dash] atlas: {len(data['meters'])} defined meters in {groups} groups "
              f"· snapshots: {snaps}")
    else:
        print(f"[dash] atlas: no catalogue at {args.meters} — the ATLAS tab will be empty.")
    sc = data["scope"]
    print(f"[dash] {data['fill_count']} fills · {sc['sessions_used']} of {sc['sessions_seen']} "
          f"sessions fit to aggregate · {len(data['symbols'])} symbols")
    for reason, n in sc["excluded"]:
        print(f"[dash]   excluded {n:>3}  {reason}")
    print(f"[dash] curve {data['curve']['sid']}: {len(data['curve']['points'])} marks, "
          f"sortino/dd {data['curve_metrics']['sod']:.1f}, maxdd {data['curve_metrics']['maxdd']:.3%}")
    print(f"[dash] trades: {t['scope']} — win {t['win_rate']:.0%}, "
          f"profit factor {t['profit_factor']:.2f}")
    print(f"[dash] approvals {a['total']}: {a['verdicts']} · {a['signed']} re-verifiable, "
          f"{a['unsigned_decided']} verdict-only · median {a['median_s']}s")
    print(f"[dash] gate denials {data['rejection_total']} across {len(data['rejections'])} reasons")
    return 0


if __name__ == "__main__":
    sys.exit(main())
