"""Correlation-aware, regime-scaled portfolio allocator.

Given each candidate's return history and its (cost-adjusted) score, produce target weights:

1. **Edge** — only positive-score names get risk; the risk budget is proportional to score.
2. **Risk-adjust** — divide the budget by volatility, so a given budget buys the same risk in a
   quiet name as a wild one (an inverse-vol tilt).
3. **De-crowd** — divide by a correlation-crowding factor (the row-sum of positive correlations),
   so a name that moves with many others is down-weighted; two identical names split one name's
   worth of weight rather than each getting a full slot.
4. **Cap** — per-name and per-sleeve caps, renormalized.
5. **Scale by regime** — multiply gross exposure by the 0-1 regime scalar; the remainder is cash.

Weights sum to ``gross`` (= regime scalar), not 1: the book runs de-risked when the regime says so.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class AllocationResult:
    weights: dict[str, float]          # name -> target weight (sum == gross_exposure)
    gross_exposure: float              # sum of weights (<= 1)
    cash: float                        # 1 - gross_exposure
    sleeve_weights: dict[str, float]
    effective_positions: float         # 1 / sum(w_normalized^2): the diversification number

    def top(self, n: int = 10) -> list[tuple[str, float]]:
        return sorted(self.weights.items(), key=lambda kv: -kv[1])[:n]


class PortfolioAllocator:
    def __init__(self, max_weight: float = 0.15, max_sleeve_weight: float = 0.6,
                 min_score: float = 0.0, vol_floor: float = 0.05) -> None:
        self.max_weight = max_weight
        self.max_sleeve_weight = max_sleeve_weight
        self.min_score = min_score
        self.vol_floor = vol_floor

    def allocate(self, returns: Mapping[str, pd.Series], scores: Mapping[str, float], *,
                 regime_scalar: float = 1.0, sleeves: Mapping[str, str] | None = None) -> AllocationResult:
        names = [n for n in returns if scores.get(n, 0.0) > self.min_score]
        if not names:
            return AllocationResult({}, 0.0, 1.0, {}, 0.0)
        sleeves = sleeves or {}

        R = pd.concat({n: returns[n] for n in names}, axis=1).dropna()
        vol = (R.std() * np.sqrt(252.0)).clip(lower=self.vol_floor)
        corr = R.corr().fillna(0.0).clip(lower=0.0)          # positive correlation = crowding

        edge = pd.Series({n: max(scores[n] - self.min_score, 0.0) for n in names})
        rw = edge / vol                                       # risk-adjusted edge
        crowd = corr.dot(rw) / rw.replace(0.0, np.nan)        # portfolio-relative crowding, >=1-ish
        crowd = crowd.reindex(names).fillna(1.0).clip(lower=1.0)
        w = (rw / crowd)
        w = (w / w.sum()).clip(lower=0.0)

        w = self._cap_names(w)
        if sleeves:
            w = self._cap_sleeves(w, sleeves)

        gross = float(np.clip(regime_scalar, 0.0, 1.0))
        weights = {n: float(w[n] * gross) for n in names if w[n] > 1e-9}
        sleeve_w: dict[str, float] = {}
        for n, wt in weights.items():
            sleeve_w[sleeves.get(n, "default")] = sleeve_w.get(sleeves.get(n, "default"), 0.0) + wt
        wn = np.array(list(w[w > 1e-9]))
        eff = float(1.0 / np.sum(wn ** 2)) if wn.size else 0.0
        return AllocationResult(weights=weights, gross_exposure=sum(weights.values()),
                                cash=1.0 - sum(weights.values()), sleeve_weights=sleeve_w,
                                effective_positions=eff)

    def _cap_names(self, w: pd.Series) -> pd.Series:
        """Water-fill each weight to <= max_weight: freeze names that hit the cap, share the
        remaining budget among the rest in proportion to their base weight, and iterate. (Simply
        clipping-and-renormalizing oscillates — the excess flows back to the name just capped.)"""
        cap = self.max_weight
        base = (w / w.sum()).clip(lower=0.0)
        fixed = pd.Series(False, index=base.index)
        out = base.copy()
        for _ in range(len(base) + 2):
            out[fixed] = cap
            unfixed = ~fixed
            free_budget = 1.0 - cap * float(fixed.sum())
            pool = float(base[unfixed].sum())
            if pool <= 0 or free_budget <= 0:
                break
            out[unfixed] = free_budget * base[unfixed] / pool
            newly = unfixed & (out > cap + 1e-12)
            if not newly.any():
                break
            fixed = fixed | newly
        return out

    def _cap_sleeves(self, w: pd.Series, sleeves: Mapping[str, str]) -> pd.Series:
        """Trim any sleeve over its cap down to the cap; the freed weight becomes cash rather than
        being forced into another sleeve (which would re-inflate a name past its own cap). Scaling
        only ever reduces weights, so the per-name caps applied earlier still hold."""
        w = w.copy()
        grp = pd.Series({n: sleeves.get(n, "default") for n in w.index})
        by = w.groupby(grp).sum()
        for sl, total in by.items():
            if total > self.max_sleeve_weight + 1e-12:
                members = grp[grp == sl].index
                w[members] = w[members] * (self.max_sleeve_weight / total)
        return w
