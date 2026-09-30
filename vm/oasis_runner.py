"""Runs ON THE VM. Reads a seed, drives OASIS, writes a result file when it finishes.

This is the wrapper the sim was missing: it owns the exit point (the result file is written in a
``finally``) and enforces the seed's budget. The driver itself is not written yet, because the OASIS
API has not been exercised from this repo; ``drive`` raises until it is filled in and tried on a
tiny budget. Needs ANTHROPIC_API_KEY in the VM's environment, never in this repo's .env.

    python oasis_runner.py seed.json --out result.json --confirm-spend
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


class Meter:
    """Running spend. ``drive`` must call ``add`` after every model call and stop on ``over``."""

    def __init__(self, max_usd: float) -> None:
        self.max_usd, self.usd, self.tin, self.tout = max_usd, 0.0, 0, 0

    def add(self, usd: float, tin: int, tout: int) -> None:
        self.usd += usd
        self.tin += tin
        self.tout += tout

    @property
    def over(self) -> bool:
        return self.usd >= self.max_usd


# USD per million tokens (input, output). An ASSUMPTION to check against the Anthropic console: a
# model missing from this table is refused, so the meter never runs on a guessed price.
PRICE = {"claude-haiku-4-5-20251001": (1.0, 5.0)}
# OASIS does not report token usage per call, so each LLMAction is charged at an assumed size, then
# doubled. The real ledger is the Anthropic console; compare it with the result's spend_usd.
EST_IN_TOKENS, EST_OUT_TOKENS, SAFETY = 4000, 300, 2.0
READOUT_BATCH = 20                    # agents per stance-readout call (real usage is metered there)

PERSONAS = [
    ("macro trader", "trades rates, FX and index futures on geopolitical and macro news"),
    ("crypto retail holder", "holds BTC and large-cap alts, reacts to fear/greed and headlines"),
    ("commodities analyst", "covers energy, metals and softs, follows supply disruptions"),
    ("risk manager", "cares about drawdowns, correlations and position limits"),
    ("equity PM", "runs a concentrated equity book and watches index volatility"),
    ("skeptic", "distrusts consensus narratives and looks for what the crowd is missing"),
    ("quant", "reads signals statistically and ignores stories without numbers"),
    ("journalist", "reports on geopolitical and market events and amplifies what is newsworthy"),
]


def _brief(seed: dict) -> str:
    """The seed's snapshot as plain text. Only fields present in the seed: withheld ones are absent."""
    s = seed["snapshot"]
    bits = [f"{k.replace('_', ' ')}: {s[k]}" for k in
            ("strategic_risk", "fear_greed", "global_alert_count", "conflict_events_active",
             "natural_disasters_active", "energy_stress") if s.get(k) is not None]
    return "; ".join(bits) or "no quantitative readings"


def _step_cost(agents: int, price: tuple[float, float]) -> float:
    return agents * (EST_IN_TOKENS * price[0] + EST_OUT_TOKENS * price[1]) / 1e6 * SAFETY


def _readout(seed: dict, posts: dict[int, list[str]], meter: Meter, price: tuple[float, float]):
    """Per-agent stance per symbol from what each agent wrote, via the Anthropic SDK directly
    (its usage field is real, unlike the estimate used for OASIS's own calls)."""
    import anthropic

    client, b, syms = anthropic.Anthropic(), seed["budget"], seed["symbols"]
    by_sym: dict[str, list[float]] = {s: [] for s in syms}
    ids = sorted(posts)
    for i in range(0, len(ids), READOUT_BATCH):
        chunk = ids[i:i + READOUT_BATCH]
        body = "\n\n".join(f"AGENT {a}:\n" + "\n".join(posts[a][-6:]) for a in chunk)
        prompt = (f"Symbols: {', '.join(syms)}.\nFor each agent below, give the stance their own writing "
                  "expresses toward each symbol: -1 adverse, 0 neutral or not mentioned, +1 constructive. "
                  "Omit a symbol an agent never addressed. Reply with JSON only: "
                  '[{"agent": <id>, "stances": {"SYMBOL": <number>}}].\n\n' + body)
        r = client.messages.create(model=b["model"], max_tokens=2000,
                                   messages=[{"role": "user", "content": prompt}])
        meter.add((r.usage.input_tokens * price[0] + r.usage.output_tokens * price[1]) / 1e6,
                  r.usage.input_tokens, r.usage.output_tokens)
        text = "".join(c.text for c in r.content if getattr(c, "type", "") == "text")
        try:
            for row in json.loads(text[text.index("["): text.rindex("]") + 1]):
                for sym, v in (row.get("stances") or {}).items():
                    if sym in by_sym and isinstance(v, (int, float)) and -1 <= v <= 1:
                        by_sym[sym].append(float(v))
        except (ValueError, TypeError):
            continue                   # an unparseable batch contributes nothing, never a guess
    out = []
    for sym, vals in by_sym.items():
        if vals:
            m = sum(vals) / len(vals)
            out.append({"symbol": sym, "mean": m, "agents": len(vals),
                        "dispersion": (sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5})
    return out


def drive(seed: dict, meter: Meter) -> tuple[int, list[dict], str]:
    """Return (completed_steps, stances, stopped_by).

    Written from camel-ai/oasis's quick_start.py and README WITHOUT the package installed, so it is
    untested against it. Names to confirm on the VM: ModelPlatformType.ANTHROPIC and whether
    ModelFactory accepts the seed's model id as a string; the ``post`` table columns. Run it first
    on about $1 and a handful of agents.
    """
    b = seed["budget"]
    price = PRICE.get(b["model"])
    if price is None:
        raise RuntimeError(f"no price for {b['model']}: add it to PRICE before spending anything")
    n, max_steps = b["max_agents"], b["max_steps"]
    readout_reserve = -(-n // READOUT_BATCH) * (6000 * price[0] + 1500 * price[1]) / 1e6 * SAFETY
    step_cost = _step_cost(n, price)
    if step_cost + readout_reserve > b["max_usd"]:
        raise RuntimeError(f"budget ${b['max_usd']} cannot cover one step (${step_cost:.2f}) plus the "
                           f"readout (${readout_reserve:.2f}); lower max_agents")

    import asyncio
    import os
    import sqlite3

    import oasis
    from camel.models import ModelFactory
    from camel.types import ModelPlatformType
    from oasis import ActionType, AgentGraph, LLMAction, ManualAction, SocialAgent, UserInfo

    async def run() -> tuple[int, str, dict[int, list[str]]]:
        model = ModelFactory.create(model_platform=ModelPlatformType.ANTHROPIC, model_type=b["model"])
        acts = [ActionType.CREATE_POST, ActionType.CREATE_COMMENT, ActionType.LIKE_POST, ActionType.FOLLOW]
        graph = AgentGraph()
        for i in range(n):
            role, desc = PERSONAS[i % len(PERSONAS)]
            graph.add_agent(SocialAgent(
                agent_id=i, agent_graph=graph, model=model, available_actions=acts,
                user_info=UserInfo(user_name=f"agent{i}", name=f"{role} {i}", profile=None,
                                   description=f"A {role}: {desc}.", recsys_type="reddit")))
        db = os.path.abspath(f"oasis_{seed['run_id']}.db")
        os.environ["OASIS_DB_PATH"] = db
        if os.path.exists(db):
            os.remove(db)
        env = oasis.make(agent_graph=graph, platform=oasis.DefaultPlatformType.REDDIT, database_path=db)
        steps, stopped = 0, "steps"
        try:
            await env.reset()
            await env.step({graph.get_agent(0): [ManualAction(
                action_type=ActionType.CREATE_POST,
                action_args={"content": f"Desk brief as of {seed['as_of']}. {_brief(seed)}. "
                                        f"Watching: {', '.join(seed['symbols'])}."})]})
            while steps < max_steps:
                if meter.usd + step_cost + readout_reserve > b["max_usd"]:
                    stopped = "budget"
                    break
                await env.step({a: LLMAction() for _, a in env.agent_graph.get_agents()})
                meter.add(step_cost, n * EST_IN_TOKENS, n * EST_OUT_TOKENS)   # the doubled estimate
                steps += 1
        finally:
            await env.close()
        con = sqlite3.connect(db)
        posts: dict[int, list[str]] = {}
        for uid, content in con.execute("SELECT user_id, content FROM post"):
            posts.setdefault(int(uid), []).append(str(content))
        con.close()
        return steps, stopped, posts

    steps, stopped, posts = asyncio.run(run())
    return steps, _readout(seed, posts, meter, price), stopped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("seed", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--confirm-spend", action="store_true")
    a = ap.parse_args()
    seed = json.loads(a.seed.read_text(encoding="utf-8"))
    b = seed["budget"]
    if not a.confirm_spend:
        print(f"dry run: would spend up to ${b['max_usd']} on {b['model']}; pass --confirm-spend")
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set on this VM", file=sys.stderr)
        return 2
    meter, steps, stances, stopped = Meter(b["max_usd"]), 0, [], "error"
    try:
        steps, stances, stopped = drive(seed, meter)
    finally:
        a.out.write_text(json.dumps({
            "run_id": seed["run_id"], "finished_at": datetime.now(UTC).isoformat(),
            "completed_steps": steps, "agents": b["max_agents"], "model": b["model"],
            "spend_usd": round(meter.usd, 4), "input_tokens": meter.tin, "output_tokens": meter.tout,
            "stopped_by": stopped, "stances": stances}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
