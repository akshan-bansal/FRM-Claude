"""AI strategy-risk model — predict a strategy's near-term drawdown risk and de-risk ahead of it.

The regime classifier reads the *market*; the WorldMonitor overlay reads *live OSINT*; this model
reads the **strategy's own return stream**. From causal features of that stream (trailing vol and its
term structure, current drawdown and time spent in it, hit rate, return skew, equity momentum,
autocorrelation) a gradient-boosted classifier predicts the probability that the strategy is about to
suffer a drawdown over the next ``horizon`` bars. That probability becomes a **risk scalar** in
``[floor, 1]`` — full size when calm, stood down when a drawdown is likely — multiplied into exposure
exactly where the regime and OSINT scalars already are.

Unlike the OSINT overlay, this layer *is* backtestable: the features and label come from history, so
it is evaluated with a **purged, expanding walk-forward** (embargo between train and test) and its
out-of-sample skill is compared honestly against a naive trailing-volatility baseline. Strategy-level
forward prediction is a low-signal problem — the walk-forward and the baseline comparison are there to
keep us honest about whether the model actually adds anything over "de-risk when vol is high."
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

# Floor for the shipped strategy-risk scalar: the gate may cut exposure to at most this fraction
# (0.75 = a 25% maximum trim). Set deliberately gentle — the vol rule is a risk *tilt*, not a
# stop, and an over-aggressive floor turns normal volatility into a de-facto flat book.
STRATEGY_RISK_FLOOR: float = 0.75

FEATURES: list[str] = [
    "ret_21", "vol_21", "vol_63", "vol_ratio", "downside_21",
    "drawdown", "time_in_dd", "hit_21", "skew_63", "equity_slope_63", "autocorr_21",
]


def build_features(returns: pd.Series) -> pd.DataFrame:
    """Causal per-bar features from a strategy return series (each row uses only info through ``t``)."""
    r = returns.astype(float).fillna(0.0)
    equity = (1.0 + r).cumprod()
    peak = equity.cummax()
    dd = equity / peak - 1.0

    # bars since the last equity peak (time spent underwater)
    at_peak = equity >= peak - 1e-12
    grp = at_peak.cumsum()
    time_in_dd = (~at_peak).groupby(grp).cumsum()

    vol_21 = r.rolling(21).std(ddof=0)
    vol_63 = r.rolling(63).std(ddof=0)
    downside = r.where(r < 0, 0.0)
    feats = pd.DataFrame({
        "ret_21": r.rolling(21).sum(),
        "vol_21": vol_21,
        "vol_63": vol_63,
        "vol_ratio": vol_21 / vol_63.replace(0.0, np.nan),
        "downside_21": downside.rolling(21).std(ddof=0),
        "drawdown": dd,
        "time_in_dd": time_in_dd.astype(float),
        "hit_21": (r > 0).rolling(21).mean(),
        "skew_63": r.rolling(63).skew(),
        "equity_slope_63": equity.pct_change(63),
        "autocorr_21": r.rolling(21).apply(lambda x: pd.Series(x).autocorr(lag=1), raw=False),
    })
    return feats.replace([np.inf, -np.inf], np.nan)


def vol_risk_scalar(returns: pd.Series, *, window: int = 21, lookback: int = 252,
                    floor: float = STRATEGY_RISK_FLOOR) -> pd.Series:
    """Trailing-volatility de-risk scalar in ``[floor, 1]`` — the robust, parsimonious mitigation.

    A causal rule: rank the strategy's trailing ``window``-bar volatility against its own recent
    ``lookback`` history and cut exposure as that percentile rises. In an honest walk-forward across
    real strategies this simple rule is a *better* forward-drawdown predictor than a gradient-boosted
    model on richer features (see :class:`StrategyRiskModel` and the demo), so it is the recommended
    strategy-risk scalar to actually ship. The classifier stays as the harness that measured this.
    """
    r = returns.astype(float).fillna(0.0)
    vol = r.rolling(window).std(ddof=0)
    pct = vol.rolling(lookback, min_periods=window * 2).rank(pct=True)
    scalar = 1.0 - pct * (1.0 - floor)
    return scalar.clip(floor, 1.0).fillna(1.0)


def scalar_from_signals(signals: pd.DataFrame, *, atr_stop_mult: float | None = None,
                        trail_atr_mult: float | None = None, time_stop_bars: int | None = None,
                        floor: float = STRATEGY_RISK_FLOOR) -> float:
    """Latest :func:`vol_risk_scalar` for a strategy, derived from a signal frame it already produced.

    Materializes the position track, forms the strategy's own return stream
    (``position_{t-1} * bar_return_t``, gross of cost — this measures *risk*, not performance), and
    returns the current scalar. Lets the live monitor gate on strategy risk without a second data
    fetch or a stored model.
    """
    from trading_live_claude.signals.generator import SignalSet

    pos = SignalSet(signals).to_positions(atr_stop_mult=atr_stop_mult, trail_atr_mult=trail_atr_mult,
                                          time_stop_bars=time_stop_bars)
    bar_ret = signals["close"].astype(float).pct_change().fillna(0.0)
    strat_ret = pos.shift(1).fillna(0.0) * bar_ret
    return float(vol_risk_scalar(strat_ret, floor=floor).iloc[-1])


def forward_drawdown_label(returns: pd.Series, horizon: int, dd_threshold: float) -> pd.Series:
    """1 when the path over ``(t, t+horizon]`` drops ``dd_threshold`` below equity at ``t`` (a risk event).

    Uses future bars — for TRAINING TARGETS ONLY, never as a feature. The walk-forward keeps every
    label strictly out of the training window that predicts it.
    """
    r = returns.astype(float).fillna(0.0).to_numpy()
    n = len(r)
    growth = 1.0 + r
    label = np.zeros(n)
    for t in range(n):
        end = min(n, t + horizon + 1)
        if end <= t + 1:
            label[t] = np.nan
            continue
        path = np.cumprod(growth[t + 1:end])   # equity relative to t, forward
        label[t] = 1.0 if path.min() <= (1.0 - dd_threshold) else 0.0
    return pd.Series(label, index=returns.index)


@dataclass
class StrategyRiskConfig:
    horizon: int = 21            # forward window (bars) the risk label looks over
    dd_threshold: float = 0.05   # forward drawdown that counts as a risk event
    train_min: int = 252         # bars required before the first out-of-sample prediction
    step: int = 21               # refit / predict cadence
    embargo: int = 5             # purge gap between train end and test start (label leakage guard)
    floor: float = 0.25          # smallest risk scalar (never fully zero)


@dataclass
class StrategyRiskResult:
    frame: pd.DataFrame          # index-aligned: p_risk, scalar, label, baseline
    oos_auc: float               # model out-of-sample AUC
    baseline_auc: float          # trailing-vol baseline AUC (the bar to beat)
    n_events: int                # number of realized risk events in the OOS span
    feature_importance: dict[str, float] = field(default_factory=dict)


class StrategyRiskModel:
    """Predict forward drawdown risk for one strategy and turn it into a de-risk scalar."""

    def __init__(self, config: StrategyRiskConfig | None = None) -> None:
        self.cfg = config or StrategyRiskConfig()

    def _make(self) -> Any:  # HistGradientBoostingClassifier; Any avoids a hard import at load
        from sklearn.ensemble import HistGradientBoostingClassifier
        return HistGradientBoostingClassifier(
            max_depth=3, learning_rate=0.05, max_iter=200, l2_regularization=1.0,
            min_samples_leaf=30, early_stopping=False, random_state=0,
        )

    def walk_forward(self, returns: pd.Series) -> StrategyRiskResult:
        """Purged expanding walk-forward. Returns OOS risk probabilities, scalars, and honest AUCs."""
        cfg = self.cfg
        X = build_features(returns)
        y = forward_drawdown_label(returns, cfg.horizon, cfg.dd_threshold)
        idx = returns.index

        p = pd.Series(np.nan, index=idx)
        base = pd.Series(np.nan, index=idx)
        n = len(idx)
        start = cfg.train_min
        while start < n:
            stop = min(start + cfg.step, n)
            # training window ends `embargo + horizon` before the test block so no training label
            # can peek into the test period.
            train_end = start - cfg.embargo - cfg.horizon
            if train_end <= 30:
                start = stop
                continue
            tr = slice(0, train_end)
            te = slice(start, stop)
            Xtr, ytr = X.iloc[tr], y.iloc[tr]
            mask = ytr.notna() & Xtr.notna().all(axis=1)
            if mask.sum() < 60 or ytr[mask].nunique() < 2:
                start = stop
                continue
            model = self._make()
            model.fit(Xtr[mask].to_numpy(), ytr[mask].astype(int).to_numpy())
            Xte = X.iloc[te]
            ok = Xte.notna().all(axis=1)
            if ok.any():
                pred = model.predict_proba(Xte[ok].to_numpy())[:, 1]
                p.iloc[start:stop] = pd.Series(pred, index=Xte.index[ok]).reindex(Xte.index).to_numpy()
            # baseline: trailing 21-bar vol, rank-normalized on the training window (higher vol -> higher risk)
            v = X["vol_21"]
            lo, hi = v.iloc[tr].min(), v.iloc[tr].max()
            base.iloc[start:stop] = ((v.iloc[te] - lo) / (hi - lo + 1e-12)).clip(0, 1).to_numpy()
            start = stop

        scalar = (1.0 - p).clip(cfg.floor, 1.0)
        frame = pd.DataFrame({"p_risk": p, "scalar": scalar, "label": y, "baseline": base})
        oos = frame.dropna(subset=["p_risk", "label"])
        auc = _auc(oos["label"].to_numpy(), oos["p_risk"].to_numpy()) if len(oos) else float("nan")
        b = frame.dropna(subset=["baseline", "label"])
        bauc = _auc(b["label"].to_numpy(), b["baseline"].to_numpy()) if len(b) else float("nan")
        return StrategyRiskResult(frame=frame, oos_auc=auc, baseline_auc=bauc,
                                  n_events=int(oos["label"].sum()) if len(oos) else 0)

    def fit_latest(self, returns: pd.Series) -> tuple[Any, float]:
        """Fit on all available history and return (model, current risk scalar) for the live book."""
        X = build_features(returns)
        y = forward_drawdown_label(returns, self.cfg.horizon, self.cfg.dd_threshold)
        mask = y.notna() & X.notna().all(axis=1)
        model = self._make()
        model.fit(X[mask].to_numpy(), y[mask].astype(int).to_numpy())
        last = X.iloc[[-1]]
        if last.notna().all(axis=1).iloc[0]:
            p = float(model.predict_proba(last.to_numpy())[:, 1][0])
        else:
            p = 0.0
        return model, float(np.clip(1.0 - p, self.cfg.floor, 1.0))


def _auc(y: np.ndarray, s: np.ndarray) -> float:
    """ROC AUC via the rank (Mann-Whitney) identity; robust to ties, no sklearn dependency."""
    pos = y == 1
    n_pos, n_neg = int(pos.sum()), int((~pos).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s), dtype=float)
    ranks[order] = np.arange(1, len(s) + 1)
    # average ranks for ties
    _, inv, counts = np.unique(s, return_inverse=True, return_counts=True)
    csum = np.cumsum(counts)
    avg = {i: (csum[i] - (counts[i] - 1) / 2.0) for i in range(len(counts))}
    ranks = np.array([avg[i] for i in inv])
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))
