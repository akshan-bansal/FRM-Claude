"""Cross-sectional forward-return ranker — a gradient-boosted-trees alpha layer.

The rule strategies each decide one name in isolation. This looks *across* the universe at once:
at every rebalance it builds a causal feature vector per name (multi-horizon momentum, realized
vol, RSI, distance from moving averages, drawdown, a liquidity trend), and a GBT predicts each
name's **relative** forward return (return minus the cross-sectional mean, i.e. alpha, not beta).
Ranking by that prediction and holding the top quantile is a long book the allocator can size.

Two things make this honest rather than a data-mining exercise:

* **relative target** — predicting return *minus the universe mean* forces the model to learn
  cross-sectional skill, not just "the market went up."
* **purged walk-forward** — the model is only ever trained on rows whose forward-return label has
  *fully closed before* the date being predicted (a gap of one ``horizon``), so an overlapping
  label can never leak the future. The headline metric is the out-of-sample rank information
  coefficient (Spearman of predicted vs realized relative return), plus a top-quantile-vs-universe
  backtest. Needs the optional ``ml`` extra (scikit-learn).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

_NON_FEATURE = {"date", "symbol", "fwd_ret", "fwd_ret_rel"}


def _rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0.0).rolling(window).mean()
    down = (-delta.clip(upper=0.0)).rolling(window).mean()
    rs = up / down.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def _naive(s: pd.Series) -> pd.Series:
    """Datetime series with any timezone stripped, so merges align regardless of tz-awareness."""
    t = pd.to_datetime(s)
    return t.dt.tz_localize(None) if t.dt.tz is not None else t


_FUND_FEATS = ("bm", "ey", "roe", "gross_margin")


def _align_fundamentals(df: pd.DataFrame, fund: pd.DataFrame) -> dict[str, np.ndarray]:
    """Forward-fill each numeric fundamental column onto ``df``'s bars (latest filing as of each
    date, via ``merge_asof``). Returns column -> array aligned positionally to ``df``."""
    cols = [c for c in fund.columns if c != "date"]
    left = pd.DataFrame({"t": _naive(df["time"]).to_numpy() if "time" in df.columns else np.asarray(df.index)})
    left["_i"] = np.arange(len(left))
    right = pd.DataFrame({"t": _naive(fund["date"]).to_numpy(), **{c: fund[c].astype(float).to_numpy() for c in cols}})
    merged = pd.merge_asof(left.sort_values("t"), right.sort_values("t"), on="t", direction="backward").sort_values("_i")
    return {c: np.asarray(merged[c].to_numpy(), dtype=float) for c in cols}


def build_panel(prices: dict[str, pd.DataFrame], *, horizon: int = 21,
                moms: tuple[int, ...] = (21, 63, 126, 252), vols: tuple[int, ...] = (20, 60),
                mas: tuple[int, ...] = (50, 200),
                fundamentals: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame:
    """Stack per-name causal features + a forward-return label into one long panel.

    Each row is (date, symbol, features…, fwd_ret, fwd_ret_rel). ``fwd_ret`` is the ``horizon``-day
    forward return (the label); ``fwd_ret_rel`` and the demeaned feature columns subtract the
    cross-sectional mean at each date, so the model works in relative (alpha) space.

    ``fundamentals`` (optional) maps symbol -> a ``(date, bvps)`` frame; it adds a book-to-market
    ``bm`` value feature. It is **allowed to be missing** per name (EDGAR only covers US-GAAP
    filers) — ``bm`` stays NaN for uncovered names and the GBT's native NaN handling copes, so a
    sparse fundamental never drops the rest of a name's technical rows.
    """
    frames = []
    for sym, df in prices.items():
        if "close" not in df or len(df) < max(mas) + horizon + 5:
            continue
        c = df["close"].astype(float).reset_index(drop=True)
        vol = df["volume"].astype(float).reset_index(drop=True) if "volume" in df else pd.Series(1.0, index=c.index)
        f = pd.DataFrame(index=c.index)
        for k in moms:
            f[f"mom_{k}"] = c.pct_change(k)
        r = c.pct_change()
        for k in vols:
            f[f"vol_{k}"] = r.rolling(k).std() * np.sqrt(252.0)
        f["rsi_14"] = _rsi(c, 14)
        for k in mas:
            f[f"dist_ma{k}"] = c / c.rolling(k).mean() - 1.0
        f["dd_252"] = c / c.rolling(252).max() - 1.0
        dv = c * vol
        f["dvol_ratio"] = (dv.rolling(20).mean() / dv.rolling(120).mean()).replace([np.inf, -np.inf], np.nan)
        if fundamentals and sym in fundamentals and not fundamentals[sym].empty:
            a = _align_fundamentals(df, fundamentals[sym])     # NaN-tolerant value/quality features
            close = c.to_numpy()
            if "bvps" in a:
                f["bm"] = a["bvps"] / close                    # book-to-market (value)
            if "eps" in a:
                f["ey"] = a["eps"] / close                     # earnings yield (value)
            if "roe" in a:
                f["roe"] = a["roe"]                             # return on equity (profitability)
            if "gross_margin" in a:
                f["gross_margin"] = a["gross_margin"]          # gross margin (quality)
        f["fwd_ret"] = c.pct_change(horizon).shift(-horizon)   # forward label
        f["date"] = pd.Index(df["time"]) if "time" in df.columns else c.index
        f["symbol"] = sym
        frames.append(f)
    panel = pd.concat(frames, ignore_index=True)

    # Drop rows missing any *technical* feature or the label; the fundamental features may stay NaN.
    tech = [col for col in panel.columns if col not in _NON_FEATURE and col not in _FUND_FEATS]
    panel = panel.dropna(subset=[*tech, "fwd_ret"]).reset_index(drop=True)

    # Cross-sectional demeaning: subtract the per-date mean so features and target are relative.
    feats = [col for col in panel.columns if col not in _NON_FEATURE]
    grp = panel.groupby("date")
    panel[feats] = panel[feats] - grp[feats].transform("mean")  # transform skips NaN in the mean
    panel["fwd_ret_rel"] = panel["fwd_ret"] - grp["fwd_ret"].transform("mean")
    return panel.reset_index(drop=True)


@dataclass(frozen=True)
class RankerResult:
    rank_ic: float                 # mean out-of-sample Spearman(pred, realized relative return)
    n_rebalances: int
    long_curve: np.ndarray         # cumulative growth of the top-quantile long book
    bench_curve: np.ndarray        # cumulative growth of the equal-weight universe
    ic_series: np.ndarray


class CrossSectionalRanker:
    """GBT that predicts relative forward return; evaluated by a purged walk-forward."""

    def __init__(self, *, max_depth: int = 3, max_iter: int = 300, learning_rate: float = 0.03,
                 l2_regularization: float = 1.0, min_samples_leaf: int = 40, random_state: int = 0) -> None:
        self._params: dict[str, Any] = dict(
            max_depth=max_depth, max_iter=max_iter, learning_rate=learning_rate,
            l2_regularization=l2_regularization, min_samples_leaf=min_samples_leaf, random_state=random_state)
        self.model = self._make()
        self.features_: list[str] = []

    def _make(self) -> Any:
        from sklearn.ensemble import HistGradientBoostingRegressor

        return HistGradientBoostingRegressor(**self._params)

    @staticmethod
    def _feature_cols(panel: pd.DataFrame) -> list[str]:
        return [c for c in panel.columns if c not in _NON_FEATURE]

    def walk_forward(self, panel: pd.DataFrame, *, horizon: int = 21, train_min: int = 504,
                     top_frac: float = 0.2) -> RankerResult:
        from scipy.stats import spearmanr

        feats = self._feature_cols(panel)
        dates = sorted(pd.Index(panel["date"]).unique())
        step = horizon  # non-overlapping holding periods
        ics: list[float] = []
        long_r: list[float] = []
        bench_r: list[float] = []
        i = train_min + horizon
        while i < len(dates):
            d = dates[i]
            cutoff = dates[i - horizon]                       # purge: labels must close before d
            train = panel[panel["date"] <= cutoff]
            test = panel[panel["date"] == d]
            if len(train) >= 200 and len(test) >= 5:
                model = self._make()
                model.fit(train[feats].to_numpy(dtype=float), train["fwd_ret_rel"].to_numpy(dtype=float))
                pred = model.predict(test[feats].to_numpy(dtype=float))
                actual = test["fwd_ret_rel"].to_numpy(dtype=float)
                rho = spearmanr(pred, actual).correlation
                if np.isfinite(rho):
                    ics.append(float(rho))
                order = np.argsort(-pred)
                k = max(1, int(len(pred) * top_frac))
                real = test["fwd_ret"].to_numpy(dtype=float)
                long_r.append(float(real[order[:k]].mean()))
                bench_r.append(float(real.mean()))
            i += step
        long_curve = np.cumprod(1.0 + np.array(long_r)) if long_r else np.array([1.0])
        bench_curve = np.cumprod(1.0 + np.array(bench_r)) if bench_r else np.array([1.0])
        return RankerResult(rank_ic=float(np.mean(ics)) if ics else 0.0, n_rebalances=len(ics),
                            long_curve=long_curve, bench_curve=bench_curve, ic_series=np.array(ics))

    def single_split_ic(self, panel: pd.DataFrame, *, horizon: int = 21, train_frac: float = 0.6) -> float:
        """Fast out-of-sample rank IC from one purged train/test split — cheap enough to drive a
        feature search (one fit instead of a full walk-forward per candidate subset)."""
        from scipy.stats import spearmanr

        feats = self._feature_cols(panel)
        dates = sorted(pd.Index(panel["date"]).unique())
        cut = int(len(dates) * train_frac)
        train = panel[panel["date"] <= dates[max(0, cut - horizon)]]
        test = panel[panel["date"] > dates[cut]]
        if len(train) < 200 or test["date"].nunique() < 3:
            return 0.0
        model = self._make()
        model.fit(train[feats].to_numpy(dtype=float), train["fwd_ret_rel"].to_numpy(dtype=float))
        pred = model.predict(test[feats].to_numpy(dtype=float))
        test = test.assign(_pred=pred)
        ics = test.groupby("date").apply(
            lambda g: spearmanr(g["_pred"], g["fwd_ret_rel"]).correlation, include_groups=False)
        m = float(np.nanmean(ics.to_numpy(dtype=float))) if len(ics) else 0.0
        return m if np.isfinite(m) else 0.0

    def fit_latest(self, panel: pd.DataFrame, *, horizon: int = 21) -> dict[str, float]:
        """Train on all rows whose label is realized, then score the most recent cross-section —
        the live predicted relative forward return per name (an edge the allocator can consume)."""
        feats = self._feature_cols(panel)
        dates = sorted(pd.Index(panel["date"]).unique())
        cutoff = dates[max(0, len(dates) - 1 - horizon)]
        train = panel[panel["date"] <= cutoff]
        latest = panel[panel["date"] == dates[-1]]
        self.features_ = feats
        self.model = self._make()
        self.model.fit(train[feats].to_numpy(dtype=float), train["fwd_ret_rel"].to_numpy(dtype=float))
        pred = self.model.predict(latest[feats].to_numpy(dtype=float))
        return dict(zip(latest["symbol"].tolist(), (float(p) for p in pred), strict=True))
