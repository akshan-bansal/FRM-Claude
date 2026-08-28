"""Pair selection: enumerate every candidate pair in a basket and rank the cointegrated ones.

A pairs strategy is only as good as the pair. Rather than hard-coding two symbols, this takes a
basket of ``{symbol: OHLC frame}`` and tests **all** ``C(n, 2)`` combinations for cointegration
(both orientations, keeping the more significant), returning a ranked shortlist of tradeable
pairs. :func:`pair_frame` then builds the ``close`` + ``close_b`` frame that ``KalmanPairs`` /
``PairsZScore`` consume, so selection feeds straight into the backtest.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..models.cointegration import engle_granger


@dataclass(frozen=True)
class PairCandidate:
    """One ordered pair ``y ~ x`` and its cointegration diagnostics."""

    sym_y: str
    sym_x: str
    hedge_ratio: float
    intercept: float
    pvalue: float
    adf_stat: float
    half_life: float
    n_obs: int
    cointegrated: bool

    @property
    def tradeable(self) -> bool:
        return self.cointegrated and 0.0 < self.half_life < float("inf")

    @property
    def key(self) -> str:
        return f"{self.sym_y}~{self.sym_x}"


def _closes(df: pd.DataFrame) -> pd.Series:
    """Close series indexed by time (uses a 'time' column if present, else the frame index)."""
    s = pd.Series(df["close"].to_numpy(dtype=float),
                  index=pd.Index(df["time"]) if "time" in df.columns else df.index)
    return s[~s.index.duplicated(keep="last")]


def _aligned(dfs: dict[str, pd.DataFrame], a: str, b: str) -> tuple[np.ndarray, np.ndarray]:
    """Inner-join two legs on their shared timestamps; returns log-price arrays (a, b)."""
    joined = pd.concat({a: _closes(dfs[a]), b: _closes(dfs[b])}, axis=1).dropna()
    return np.log(joined[a].to_numpy()), np.log(joined[b].to_numpy())


def _best_orientation(dfs: dict[str, pd.DataFrame], a: str, b: str, *, alpha: float,
                      max_half_life: float, min_obs: int) -> PairCandidate | None:
    la, lb = _aligned(dfs, a, b)
    n = la.shape[0]
    if n < min_obs:
        return None
    best: PairCandidate | None = None
    for sym_y, sym_x, y, x in ((a, b, la, lb), (b, a, lb, la)):
        r = engle_granger(y, x, alpha=alpha, max_half_life=max_half_life)
        cand = PairCandidate(sym_y=sym_y, sym_x=sym_x, hedge_ratio=r.hedge_ratio,
                             intercept=r.intercept, pvalue=r.pvalue, adf_stat=r.adf_stat,
                             half_life=r.half_life, n_obs=n, cointegrated=r.cointegrated)
        if best is None or cand.pvalue < best.pvalue:  # keep the more significant direction
            best = cand
    return best


def enumerate_pairs(dfs: dict[str, pd.DataFrame], *, alpha: float = 0.05,
                    max_half_life: float = 252.0, min_obs: int = 250) -> list[PairCandidate]:
    """Every ``C(n, 2)`` pair in ``dfs``, best orientation each, ranked by ascending p-value.

    Pairs with fewer than ``min_obs`` overlapping bars are skipped. Filter the result with
    :func:`.tradeable` or ``[c for c in result if c.cointegrated]`` to get the shortlist.
    """
    out: list[PairCandidate] = []
    for a, b in itertools.combinations(sorted(dfs), 2):
        cand = _best_orientation(dfs, a, b, alpha=alpha, max_half_life=max_half_life, min_obs=min_obs)
        if cand is not None:
            out.append(cand)
    out.sort(key=lambda c: (not c.tradeable, c.pvalue, c.half_life))
    return out


def select_cointegrated_pairs(dfs: dict[str, pd.DataFrame], *, alpha: float = 0.05,
                              max_half_life: float = 252.0, min_obs: int = 250,
                              top: int | None = None) -> list[PairCandidate]:
    """Just the tradeable (cointegrated, finite half-life) pairs, ranked; optionally top-N."""
    picks = [c for c in enumerate_pairs(dfs, alpha=alpha, max_half_life=max_half_life, min_obs=min_obs)
             if c.tradeable]
    return picks[:top] if top is not None else picks


def pair_frame(dfs: dict[str, pd.DataFrame], sym_y: str, sym_x: str) -> pd.DataFrame:
    """OHLCV frame for the ``y`` leg plus a ``close_b`` column from the ``x`` leg, aligned on
    shared timestamps — the exact input ``KalmanPairs`` / ``PairsZScore`` expect."""
    y_df = dfs[sym_y].copy()
    key = "time" if "time" in y_df.columns else None
    xb = _closes(dfs[sym_x]).rename("close_b")
    yi = pd.Index(y_df["time"]) if key else y_df.index
    y_df = y_df.assign(close_b=xb.reindex(yi).to_numpy())
    return y_df.dropna(subset=["close_b"]).reset_index(drop=True)
