"""Book construction from the cross-sectional ranker — the ranker's edge feeds the allocator.

Once the cross-sectional GBT clears the noise floor (which it does on a broad universe), its live
prediction — each name's expected *relative* forward return — is exactly the "edge" the allocator
wants. This ties the pieces into one call: build the feature panel, fit the ranker for the latest
cross-section, read the market regime off a benchmark, and hand the ranker's scores to the
correlation-aware, regime-scaled :class:`PortfolioAllocator`. Only positive-edge names (the top
half the ranker favors) get weight, so the result is a long book of the model's picks, de-crowded
and sized to the regime.

Paper research only — this produces target weights, it places no orders.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..backtest.costs import CostModel
from ..models import CrossSectionalRanker, RegimeClassifier, build_panel
from .allocator import AllocationResult, PortfolioAllocator


def ranker_scores(prices: Mapping[str, pd.DataFrame], *, horizon: int = 21,
                  fundamentals: Mapping[str, pd.DataFrame] | None = None,
                  ranker: CrossSectionalRanker | None = None) -> dict[str, float]:
    """The ranker's predicted relative forward return per name for the latest cross-section."""
    panel = build_panel(dict(prices), horizon=horizon, fundamentals=dict(fundamentals) if fundamentals else None)
    return (ranker or CrossSectionalRanker()).fit_latest(panel, horizon=horizon)


def build_book(prices: Mapping[str, pd.DataFrame], *, horizon: int = 21,
               fundamentals: Mapping[str, pd.DataFrame] | None = None,
               regime_benchmark: pd.Series | None = None,
               sleeves: Mapping[str, str] | None = None,
               allocator: PortfolioAllocator | None = None,
               ranker: CrossSectionalRanker | None = None) -> AllocationResult:
    """Ranker edge → allocator weights: the full book in one call.

    ``regime_benchmark`` (a broad close series) scales gross exposure; ``sleeves`` maps symbol ->
    sleeve for the sleeve caps. Returns the allocator's :class:`AllocationResult`.
    """
    scores = ranker_scores(prices, horizon=horizon, fundamentals=fundamentals, ranker=ranker)
    returns = {s: prices[s]["close"].pct_change().dropna().reset_index(drop=True) for s in prices}
    regime = RegimeClassifier().classify(regime_benchmark).risk_scalar if regime_benchmark is not None else 1.0
    alloc = allocator or PortfolioAllocator()
    return alloc.allocate(returns, scores, regime_scalar=regime, sleeves=sleeves)


@dataclass(frozen=True)
class BookBacktest:
    net_curve: np.ndarray          # cumulative growth of $1, net of cost
    gross_curve: np.ndarray        # same, gross (no cost) — the turnover drag is net minus this
    bench_curve: np.ndarray        # equal-weight, fully-invested universe
    net_return: float
    gross_return: float
    bench_return: float
    sharpe_net: float              # annualized, on the per-rebalance net returns
    max_drawdown_net: float
    avg_turnover: float            # mean sum|Δw| per rebalance (2.0 = full turnover)
    cost_drag: float               # gross_return - net_return
    n_rebalances: int


def backtest_book(prices: Mapping[str, pd.DataFrame], *, horizon: int = 21, train_min: int = 504,
                  fundamentals: Mapping[str, pd.DataFrame] | None = None, cost: CostModel | None = None,
                  allocator: PortfolioAllocator | None = None, regime_benchmark: pd.Series | None = None,
                  sleeves: Mapping[str, str] | None = None,
                  ranker: CrossSectionalRanker | None = None) -> BookBacktest:
    """Walk-forward backtest of the ranker-driven book, **net of turnover cost**.

    At each rebalance the ranker is re-fit on data whose label closed a full horizon before (purged),
    the allocator builds weights from its scores, and the book is charged ``cost`` on the turnover
    (``sum|w_new - w_old|``) it takes to get there. Returns net/gross/benchmark curves so the cost
    drag is explicit. Needs the ``ml`` extra.
    """
    cost = cost or CostModel.frictionless()
    alloc = allocator or PortfolioAllocator()
    rk = ranker or CrossSectionalRanker()
    per_side = cost.per_side_frac()

    panel = build_panel(dict(prices), horizon=horizon, fundamentals=dict(fundamentals) if fundamentals else None)
    feats = [c for c in panel.columns if c not in {"date", "symbol", "fwd_ret", "fwd_ret_rel"}]
    wide = pd.concat({s: pd.Series(prices[s]["close"].pct_change().to_numpy(),
                                   index=pd.Index(prices[s]["time"]) if "time" in prices[s] else prices[s].index)
                      for s in prices}, axis=1)
    bench = regime_benchmark

    dates = sorted(pd.Index(panel["date"]).unique())
    prev_w: dict[str, float] = {}
    net_r: list[float] = []
    gross_r: list[float] = []
    bench_r: list[float] = []
    turns: list[float] = []
    i = train_min + horizon
    while i < len(dates):
        d = dates[i]
        train = panel[panel["date"] <= dates[i - horizon]]
        test = panel[panel["date"] == d]
        if len(train) >= 200 and len(test) >= 5:
            model = rk._make()
            model.fit(train[feats].to_numpy(dtype=float), train["fwd_ret_rel"].to_numpy(dtype=float))
            scores = dict(zip(test["symbol"].tolist(),
                              (float(p) for p in model.predict(test[feats].to_numpy(dtype=float))), strict=True))
            rets = {s: wide[s].loc[:d].dropna() for s in scores if s in wide}
            regime = RegimeClassifier().classify(bench.loc[:d]).risk_scalar if bench is not None else 1.0
            w = alloc.allocate(rets, scores, regime_scalar=regime, sleeves=sleeves).weights
            fwd = dict(zip(test["symbol"].tolist(), test["fwd_ret"].to_numpy(dtype=float), strict=True))
            port = sum(wt * fwd.get(s, 0.0) for s, wt in w.items())
            turnover = sum(abs(w.get(s, 0.0) - prev_w.get(s, 0.0)) for s in set(w) | set(prev_w))
            net_r.append(port - turnover * per_side)
            gross_r.append(port)
            bench_r.append(float(np.mean(list(fwd.values()))))
            turns.append(turnover)
            prev_w = w
        i += horizon

    net_c = np.cumprod(1.0 + np.array(net_r)) if net_r else np.array([1.0])
    gross_c = np.cumprod(1.0 + np.array(gross_r)) if gross_r else np.array([1.0])
    bench_c = np.cumprod(1.0 + np.array(bench_r)) if bench_r else np.array([1.0])
    na = np.array(net_r)
    periods_per_year = 252.0 / horizon
    sharpe = float(np.mean(na) / np.std(na) * np.sqrt(periods_per_year)) if len(na) > 1 and np.std(na) > 0 else 0.0
    peak = np.maximum.accumulate(net_c)
    max_dd = float((net_c / peak - 1.0).min()) if net_c.size else 0.0
    return BookBacktest(net_curve=net_c, gross_curve=gross_c, bench_curve=bench_c,
                        net_return=float(net_c[-1] - 1.0), gross_return=float(gross_c[-1] - 1.0),
                        bench_return=float(bench_c[-1] - 1.0), sharpe_net=sharpe, max_drawdown_net=max_dd,
                        avg_turnover=float(np.mean(turns)) if turns else 0.0,
                        cost_drag=float(gross_c[-1] - net_c[-1]), n_rebalances=len(net_r))
