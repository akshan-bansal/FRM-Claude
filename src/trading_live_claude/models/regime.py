"""Market-regime overlay — a book-wide risk classifier.

Everything else in the stack decides *per symbol*; nothing looks at the market as a whole and says
"this is a bad time to be risk-on." This classifies a broad benchmark into a regime from three
causal gates and multiplies them into a single **risk scalar in [0, 1]** that the portfolio layer
uses to scale gross exposure — full size in a calm uptrend, stood down in a stressed drawdown:

* **trend gate** — price relative to its long moving average (below the MA ⇒ ramp down).
* **volatility gate** — realized vol vs its own recent history (a high vol percentile ⇒ ramp down).
* **drawdown gate** — distance from the benchmark's peak (deeper drawdown ⇒ ramp down).

All three use only trailing data and are shifted a bar, so the regime at ``t`` depends on
information through ``t-1`` — no lookahead. :meth:`series` returns the scalar over time for
backtesting the overlay; :meth:`classify` returns the latest state for the live book.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class RegimeState:
    label: str            # "risk_on" | "neutral" | "risk_off"
    risk_scalar: float    # [0, 1] — multiply gross exposure by this
    trend: float          # price / MA - 1
    vol_annual: float
    vol_pctile: float     # [0, 1]
    drawdown: float       # <= 0


def _ramp(x: float, lo: float, hi: float, y_lo: float, y_hi: float) -> float:
    """Linear ramp of x from (lo->y_lo) to (hi->y_hi), clamped outside [lo, hi]."""
    if hi == lo:
        return y_hi
    t = (x - lo) / (hi - lo)
    t = min(1.0, max(0.0, t))
    return y_lo + t * (y_hi - y_lo)


class RegimeClassifier:
    """Classify a broad benchmark price series into a risk regime.

    ``ma_window`` sets the trend MA; ``vol_window`` the realized-vol window and ``vol_lookback`` the
    history it is ranked against; ``dd_full`` the drawdown at which the drawdown gate bottoms out;
    ``floor`` the smallest risk scalar (never fully zero, so a recovering book can re-enter).
    """

    def __init__(self, ma_window: int = 150, vol_window: int = 20, vol_lookback: int = 252,
                 dd_full: float = -0.20, floor: float = 0.2) -> None:
        self.ma_window = ma_window
        self.vol_window = vol_window
        self.vol_lookback = vol_lookback
        self.dd_full = dd_full
        self.floor = floor

    def series(self, prices: pd.Series) -> pd.DataFrame:
        """Per-bar regime frame (risk_scalar, trend, vol_pctile, drawdown), causal (shifted)."""
        p = prices.astype(float).reset_index(drop=True)
        ma = p.rolling(self.ma_window, min_periods=self.ma_window // 2).mean()
        trend = (p / ma - 1.0)

        ret = p.pct_change()
        vol = ret.rolling(self.vol_window).std() * np.sqrt(252.0)
        vol_pctile = vol.rolling(self.vol_lookback, min_periods=self.vol_window * 2).rank(pct=True)

        peak = p.cummax()
        drawdown = p / peak - 1.0

        trend_gate = trend.apply(lambda t: _ramp(t, -0.10, 0.0, self.floor, 1.0) if pd.notna(t) else 1.0)
        vol_gate = vol_pctile.apply(lambda q: _ramp(q, 0.5, 0.95, 1.0, self.floor) if pd.notna(q) else 1.0)
        dd_gate = drawdown.apply(lambda d: _ramp(d, self.dd_full, 0.0, self.floor, 1.0) if pd.notna(d) else 1.0)
        scalar = (trend_gate * vol_gate * dd_gate).clip(self.floor, 1.0)

        out = pd.DataFrame({"trend": trend, "vol": vol, "vol_pctile": vol_pctile,
                            "drawdown": drawdown, "risk_scalar": scalar})
        return out.shift(1).fillna({"risk_scalar": 1.0})  # regime at t uses data through t-1

    @staticmethod
    def _label(scalar: float) -> str:
        return "risk_on" if scalar >= 0.75 else "risk_off" if scalar < 0.4 else "neutral"

    def classify(self, prices: pd.Series) -> RegimeState:
        """Latest regime state for the live book."""
        s = self.series(prices)
        last = s.iloc[-1]
        scalar = float(last["risk_scalar"])
        return RegimeState(label=self._label(scalar), risk_scalar=scalar,
                           trend=float(last["trend"]) if pd.notna(last["trend"]) else 0.0,
                           vol_annual=float(last["vol"]) if pd.notna(last["vol"]) else 0.0,
                           vol_pctile=float(last["vol_pctile"]) if pd.notna(last["vol_pctile"]) else 0.5,
                           drawdown=float(last["drawdown"]) if pd.notna(last["drawdown"]) else 0.0)
