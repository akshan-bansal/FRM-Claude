"""Per-symbol analysis behind a proposed basket: what evidence exists, how the engines map it.

Runs the repo's own engines over the symbols a human typed for a venue and returns what they say,
with no opinion added:

* venue policy: whether the symbol may trade on that venue at all (desk venue split);
* evidence: the walk-forward record (out-of-sample), or the crypto sleeve's in-sample screen, or
  nothing. In-sample and out-of-sample figures are kept in separate fields and never mixed;
* engine mapping: strategy, asset class, the live overlay's class scalar and any interpret thesis
  whose exemplars include the symbol;
* return statistics and the allocator's correlation-aware weights, from daily closes the caller
  supplies. A symbol with no closes gets no statistics: the row says why, the aggregates are
  computed over the symbols that have them, and ``scope`` states which.

Theses and figures that rest on a source older than ``MAX_SOURCE_AGE_H`` are withheld, not shown
with a caution. Pure functions: every input is a parameter, so the tests need no network.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import pandas as pd

from ..execution.basket import ib_refusal, normalize
from ..intel.interpret import interpret
from ..intel.overlay import IntelSnapshot
from ..portfolio.allocator import PortfolioAllocator
from .universe import CRYPTO_SLEEVE, validated_for

MAX_SOURCE_AGE_H = 48.0
PERIODS_PER_YEAR = {"kraken": 365, "qt": 252, "ib": 252}
MIN_BARS = 60


def _policy(venue: str, symbol: str) -> str | None:
    return ib_refusal(symbol) if venue == "ib" else None


def _evidence(symbol: str) -> dict[str, Any]:
    wf = validated_for(symbol)
    if wf is not None:
        return {"kind": "walk_forward", "tier": wf.tier, "asset_class": wf.asset_class,
                "strategy": wf.strategy, "params": dict(wf.params), "oos_score": wf.oos_score,
                "wfe": wf.wfe, "oos_return": wf.oos_return, "oos_max_drawdown": wf.oos_max_drawdown,
                "oos_trades": wf.oos_trades, "oos_win_rate": wf.oos_win_rate}
    cs = CRYPTO_SLEEVE.get(symbol)
    if cs is not None:
        # In-sample only: the endpoint's ~720 bars are too short to walk-forward.
        return {"kind": "screened_in_sample", "tier": cs.tier, "asset_class": cs.asset_class,
                "strategy": cs.strategy, "params": dict(cs.params), "screen_score": cs.screen_score}
    return {"kind": "none"}


def _stats(close: pd.Series, periods: int) -> dict[str, float] | None:
    close = close.dropna()
    if len(close) < MIN_BARS:
        return None
    r = close.pct_change().dropna()
    peak = close.cummax()
    return {"bars": len(close), "ann_vol": float(r.std(ddof=0) * math.sqrt(periods)),
            "max_drawdown": float((close / peak - 1.0).min()),
            "total_return": float(close.iloc[-1] / close.iloc[0] - 1.0)}


def _current_theses(snapshot: IntelSnapshot | None) -> tuple[list[Any], dict[str, str]]:
    """Interpret theses, minus any that rest on a source past the freshness limit."""
    if snapshot is None:
        return [], {"theses": "no intel snapshot in the overlay journal"}
    theses = [t for t in interpret(snapshot) if t.name != "No notable configuration"]
    stale = {k: v for k, v in (snapshot.source_age_hours or {}).items() if v > MAX_SOURCE_AGE_H}
    withheld: dict[str, str] = {}
    if "energy" in stale:
        kept = [t for t in theses if "energy" not in t.themes]
        if len(kept) != len(theses):
            withheld["theses:energy"] = f"energy source is {stale['energy']:.0f}h old, limit {MAX_SOURCE_AGE_H:.0f}h"
        theses = kept
    return theses, withheld


def build_report(
    venue: str,
    symbols: list[str],
    *,
    closes: Mapping[str, pd.Series] | None = None,
    snapshot: IntelSnapshot | None = None,
    overlay_decisions: Mapping[str, Mapping[str, Any]] | None = None,
    asset_class_of: Mapping[str, str] | None = None,
    launch_map: Mapping[str, str] | None = None,
    fallback_strategy: str | None = None,
) -> dict[str, Any]:
    """Analysis for one venue's proposed symbols. ``closes`` maps symbol -> daily close series."""
    venue = venue.lower()
    closes = closes or {}
    periods = PERIODS_PER_YEAR.get(venue, 252)
    theses, withheld = _current_theses(snapshot)
    rows: list[dict[str, Any]] = []
    for sym in normalize(symbols):
        refusal = _policy(venue, sym)
        ev = _evidence(sym)
        klass = ev.get("asset_class") or (asset_class_of or {}).get(sym)
        dec = (overlay_decisions or {}).get(klass) if klass else None
        stats = _stats(closes[sym], periods) if sym in closes else None
        implicated = [t.name for t in theses if sym in t.exemplars()]
        rows.append({
            "symbol": sym, "policy_refusal": refusal, "evidence": ev, "asset_class": klass,
            "strategy": ev.get("strategy") or (launch_map or {}).get(sym) or fallback_strategy,
            # Where the mapping comes from. A launch-map or fallback strategy is what the session
            # RUNS, not something validated: it has no out-of-sample record behind it.
            "strategy_source": ("evidence" if ev.get("strategy") else
                                "launch_map" if (launch_map or {}).get(sym) else
                                "fallback" if fallback_strategy else None),
            "overlay": ({"scalar": dec.get("scalar"), "halt": dec.get("halt")} if dec else None),
            "theses": implicated,
            "stats": stats,
            "stats_missing": None if stats else
                ("no daily closes supplied for this symbol" if sym not in closes else
                 f"fewer than {MIN_BARS} bars"),
        })
    usable = {r["symbol"]: closes[r["symbol"]] for r in rows if r["stats"] and not r["policy_refusal"]}
    corr: dict[str, Any] | None = None
    weights: dict[str, float] = {}
    if len(usable) >= 2:
        rets = pd.DataFrame({k: v.pct_change() for k, v in usable.items()}).dropna()
        if len(rets) >= MIN_BARS:
            cm = rets.corr()
            pairs = [(a, b, float(cm.loc[a, b])) for i, a in enumerate(cm.columns) for b in cm.columns[i + 1:]]
            hi = max(pairs, key=lambda p: p[2])
            corr = {"mean_pairwise": sum(p[2] for p in pairs) / len(pairs), "max_pair": [hi[0], hi[1]],
                    "max_value": hi[2], "overlap_bars": len(rets)}
            scores = {r["symbol"]: float(r["evidence"].get("oos_score") or r["evidence"].get("screen_score") or 0.0)
                      for r in rows if r["symbol"] in usable}
            res = PortfolioAllocator(max_weight=0.30, max_sleeve_weight=1.0, min_score=0.0).allocate(
                {k: v.pct_change().dropna() for k, v in usable.items()}, scores, regime_scalar=1.0)
            weights = dict(res.weights)
    return {
        "venue": venue, "rows": rows, "correlation": corr, "allocator_weights": weights,
        "withheld": withheld,
        "scope": {"symbols": len(rows), "with_stats": len(usable),
                  "walk_forward": sum(1 for r in rows if r["evidence"]["kind"] == "walk_forward"),
                  "in_sample_only": sum(1 for r in rows if r["evidence"]["kind"] == "screened_in_sample"),
                  "no_evidence": sum(1 for r in rows if r["evidence"]["kind"] == "none"),
                  "refused": sum(1 for r in rows if r["policy_refusal"])},
    }
