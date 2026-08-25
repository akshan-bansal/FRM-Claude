"""Alternative risk estimates for the heat gate.

The router's heat gate consumes a single scalar — "open risk in dollars" — and never needs
to know how it was produced. This module supplies two swappable ways to produce it, so the
gate can budget on tail risk and credit diversification without any change to its contract:

  * :func:`per_trade_risk` — how much a single position risks. The default ``"atr"`` model is
    the ATR-stop loss (``shares x stop_distance``); ``"var"`` / ``"cvar"`` instead take the
    alpha-quantile / Expected-Shortfall of the name's own returns, capturing the fat left
    tail a symmetric ATR range misses.
  * :func:`portfolio_risk` — how open positions aggregate. ``"sum"`` is the naive,
    correlation-blind total (the current behaviour); ``"corr"`` combines the per-position
    dollar risks through the correlation of their returns, ``sqrt(r' rho r)``, so two
    correlated bank positions no longer count as fully independent risk.

Both are pure functions of the numbers/return series handed in, so they run and test with no
broker. Missing or too-short return history degrades safely to the ATR estimate / the sum.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

import numpy as np
import pandas as pd

from .tail import expected_shortfall, value_at_risk

RiskModel = Literal["atr", "var", "cvar"]
HeatAggregation = Literal["sum", "corr"]

_MIN_OBS = 20  # returns shorter than this can't support a tail/correlation estimate


def per_trade_risk(
    shares: float,
    price: float,
    *,
    stop_distance: float | None = None,
    returns: pd.Series | None = None,
    model: RiskModel = "atr",
    alpha: float = 0.05,
) -> float:
    """Dollar risk of one position under ``model`` (see module docstring).

    Falls back to the ATR estimate when returns are missing or too short to estimate a tail.
    """
    qty = abs(float(shares))
    clean = returns.dropna() if returns is not None else None
    if model == "atr" or clean is None or len(clean) < _MIN_OBS:
        if stop_distance is None or stop_distance <= 0:
            return 0.0
        return qty * float(stop_distance)
    pct = value_at_risk(clean, alpha) if model == "var" else expected_shortfall(clean, alpha)
    return qty * abs(float(price)) * abs(float(pct))


def portfolio_risk(
    risks: Mapping[str, float],
    returns: Mapping[str, pd.Series | None],
    *,
    method: HeatAggregation = "sum",
    alpha: float = 0.05,
) -> float:
    """Aggregate per-position dollar ``risks`` into one open-risk number for the heat gate.

    ``"sum"`` totals them (assumes everything moves together). ``"corr"`` combines the risks
    of names with overlapping return history via ``sqrt(r' rho r)`` — crediting
    diversification — and adds any name lacking history at its standalone risk.
    """
    positive = {s: float(r) for s, r in risks.items() if r and r > 0}
    total = sum(positive.values())
    if method == "sum" or len(positive) < 2:
        return float(total)

    cols: dict[str, pd.Series] = {}
    for s in positive:
        ser = returns.get(s)
        if ser is None:
            continue
        clean = ser.dropna()
        if len(clean) >= _MIN_OBS:
            cols[s] = clean
    if len(cols) < 2:
        return float(total)

    frame = pd.DataFrame(cols).dropna()
    if len(frame) < _MIN_OBS:
        return float(total)

    rho = frame.corr().to_numpy()
    r = np.array([positive[s] for s in frame.columns], dtype=float)
    combined = float(np.sqrt(max(float(r @ rho @ r), 0.0)))
    # Names without usable history contribute their standalone risk additively.
    modeled = float(r.sum())
    return combined + max(total - modeled, 0.0)
