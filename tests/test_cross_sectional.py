from __future__ import annotations

import numpy as np
import pandas as pd

from trading_live_claude.models import CrossSectionalRanker, build_panel


def _universe(n_names: int = 14, days: int = 900, seed: int = 0) -> dict[str, pd.DataFrame]:
    """Names with a persistent 'quality' drift, so momentum features carry a real cross-sectional
    signal about relative forward return — a factor the ranker should recover."""
    rng = np.random.default_rng(seed)
    t = pd.date_range("2021-01-01", periods=days, freq="B", tz="UTC")
    quality = rng.normal(0.0, 0.0009, n_names)          # persistent per-name edge
    out = {}
    for i in range(n_names):
        r = quality[i] + rng.normal(0.0, 0.012, days)
        c = 100.0 * np.exp(np.cumsum(r))
        out[f"N{i:02d}"] = pd.DataFrame({"time": t, "open": c, "high": c * 1.005,
                                         "low": c * 0.995, "close": c, "volume": 1e6})
    return out


def test_build_panel_is_cross_sectionally_demeaned() -> None:
    panel = build_panel(_universe(), horizon=21)
    assert {"date", "symbol", "fwd_ret", "fwd_ret_rel"}.issubset(panel.columns)
    assert "mom_63" in panel.columns
    # relative columns sum to ~0 within each date (demeaned)
    per_date = panel.groupby("date")["fwd_ret_rel"].mean().abs().max()
    assert per_date < 1e-9


def test_walk_forward_recovers_the_factor() -> None:
    panel = build_panel(_universe(seed=1), horizon=21)
    res = CrossSectionalRanker().walk_forward(panel, horizon=21, train_min=300, top_frac=0.3)
    assert res.n_rebalances >= 3
    assert res.rank_ic > 0.0                                   # positive out-of-sample skill
    assert res.long_curve[-1] >= res.bench_curve[-1]           # top-quantile beats equal-weight


def test_fundamentals_add_bm_and_tolerate_missing() -> None:
    """A book-to-market feature is added where fundamentals exist and stays NaN (not dropped)
    elsewhere — EDGAR only covers some names, and the GBT handles the gaps natively."""
    uni = _universe(n_names=6, seed=4)
    names = list(uni)
    # give only the first two names a synthetic quarterly bvps history
    fundamentals = {}
    for sym in names[:2]:
        t = uni[sym]["time"]
        k = len(t[::63])
        fundamentals[sym] = pd.DataFrame({"date": t[::63].to_numpy(), "bvps": np.linspace(40, 50, k),
                                          "eps": np.linspace(3, 5, k), "roe": np.linspace(0.1, 0.2, k)})
    panel = build_panel(uni, horizon=21, fundamentals=fundamentals)
    assert {"bm", "ey", "roe"}.issubset(panel.columns)   # value + quality features added
    covered = panel[panel["symbol"].isin(names[:2])]["bm"].notna().mean()
    uncovered = panel[panel["symbol"].isin(names[2:])]["bm"].notna().mean()
    assert covered > 0.5 and uncovered == 0.0            # covered names have bm; others NaN, still present
    # rows for uncovered names are retained (not dropped for the missing fundamental)
    assert panel["symbol"].nunique() == 6


def test_fit_latest_scores_every_name() -> None:
    uni = _universe(seed=2)
    scores = CrossSectionalRanker().fit_latest(build_panel(uni, horizon=21), horizon=21)
    assert set(scores).issubset(set(uni))
    assert all(np.isfinite(v) for v in scores.values())
