"""Run mean-reversion strategies on equity-valuation ratios, and overlap them with price.

Three composable pieces, all built on ``signals.valuation``:

  * ``ValuationStrategy`` — runs a mean-reversion base Strategy on a valuation series
    (default price-to-book) *instead of* price. A Bollinger/RSI mean-revert then buys
    when the stock is unusually **cheap** on P/B and turning up, not when its price dips.
  * ``ValuationGateOverlay`` — the **precision** overlap: a price strategy's entry
    survives only when valuation is on the cheap side of its trailing distribution
    (P/B below its rolling mean). Mirrors ``overlay.ConfirmOverlay``, but the confirming
    signal is fundamental, not a candlestick.
  * ``valuation_composite`` — the **recall** overlap: unions a price mean-reversion
    strategy with its valuation-run twin, widening the candidate set.

Book values arrive via a ``bvps`` column / provider; with none attached P/B collapses to
the price level (``signals.valuation.DEFAULT_BVPS``), so every strategy stays runnable on
plain OHLCV and lights up once fundamentals are wired. Past-only throughout — the z-score
and ratios reference only bars up to the current one, so no lookahead is introduced beyond
the base strategy's own.
"""
from __future__ import annotations

import pandas as pd

from ..signals.indicators import atr
from ..signals.valuation import VALUATION_METRICS, BvpsSource, rolling_zscore, valuation_series
from .base import Strategy, StrategyContext
from .composite import CompositeStrategy
from .examples.bollinger import BollingerMeanRevert
from .examples.rsi_meanrevert import RsiMeanRevert


class ValuationStrategy(Strategy):
    """Run a mean-reversion ``base`` on a valuation ratio rather than on price."""

    name = "valuation"
    description = "Mean-reversion base run on an equity-valuation ratio (P/B by default)"

    def __init__(
        self, base: Strategy, metric: str = "price_to_book", bvps: BvpsSource = None, atr_window: int = 14
    ) -> None:
        super().__init__(base=base.name, metric=metric)
        if metric not in VALUATION_METRICS:
            raise ValueError(f"Unknown valuation metric {metric!r}; choose from {VALUATION_METRICS}")
        self.base = base
        self.metric = metric
        self.bvps = bvps
        self.atr_window = atr_window
        self.stop_atr_mult = base.stop_atr_mult

    def required_history_bars(self) -> int:
        return max(self.base.required_history_bars(), 60)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        ratio = valuation_series(df, self.metric, self.bvps)
        # Feed the ratio to the base as if it were the price series.
        vdf = df.copy()
        for col in ("open", "high", "low", "close"):
            vdf[col] = ratio.to_numpy()
        sig = self.base.generate_signals(vdf, ctx)

        out = df.copy()
        out["valuation_ratio"] = ratio.to_numpy()
        out["entry"] = sig["entry"].to_numpy()
        out["exit"] = sig["exit"].to_numpy()
        out["atr"] = atr(df, self.atr_window)  # sizing uses the real price ATR, not the ratio's
        out["signal_strength"] = (
            sig["signal_strength"].to_numpy() if "signal_strength" in sig.columns else out["entry"].astype(float)
        )
        return out


class ValuationGateOverlay(Strategy):
    """Gate a price strategy's entries on cheap valuation (precision overlap)."""

    name = "valuation_gate"
    description = "Price strategy entries confirmed only when valuation is cheap"

    def __init__(
        self,
        base: Strategy,
        metric: str = "price_to_book",
        bvps: BvpsSource = None,
        z_window: int = 63,
        max_z: float = 0.0,
    ) -> None:
        super().__init__(base=base.name, metric=metric, z_window=z_window, max_z=max_z)
        if metric not in VALUATION_METRICS:
            raise ValueError(f"Unknown valuation metric {metric!r}; choose from {VALUATION_METRICS}")
        self.base = base
        self.metric = metric
        self.bvps = bvps
        self.z_window = z_window
        self.max_z = max_z
        self.stop_atr_mult = base.stop_atr_mult

    def required_history_bars(self) -> int:
        return max(self.base.required_history_bars(), self.z_window + 5)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = self.base.generate_signals(df, ctx).copy()
        ratio = valuation_series(df, self.metric, self.bvps)
        z = rolling_zscore(ratio, self.z_window)
        # Cheap = below the trailing mean on P/B (low), or above it on B/M (high).
        cheap = (z <= self.max_z) if self.metric == "price_to_book" else (z >= -self.max_z)
        cheap = cheap.fillna(False)

        base_entry = out["entry"].fillna(0).astype(bool)
        gated = base_entry & cheap
        out["entry"] = gated.astype(int)
        out["valuation_z"] = z.to_numpy()
        if "signal_strength" in out.columns:
            out["signal_strength"] = out["signal_strength"].where(gated, 0.0)
        if "atr" not in out.columns:
            out["atr"] = atr(df, 14)
        return out


def valuation_composite(
    price_base: Strategy | None = None, metric: str = "price_to_book", bvps: BvpsSource = None
) -> CompositeStrategy:
    """Recall overlap: union a price mean-reversion strategy with its valuation-run twin."""
    price_leg = price_base or BollingerMeanRevert()
    valuation_leg = ValuationStrategy(BollingerMeanRevert(), metric=metric, bvps=bvps)
    return CompositeStrategy([price_leg, valuation_leg])


# --- ready-made, zero-arg registrations (robust params baked in; P/B by default) --- #


class _ValBollinger(ValuationStrategy):
    name = "val_bollinger"
    description = "Bollinger mean-reversion run on price-to-book (buy when cheap)"

    def __init__(self) -> None:
        super().__init__(base=BollingerMeanRevert(window=30, n_std=3.0), metric="price_to_book")


class _ValRsi(ValuationStrategy):
    name = "val_rsi"
    description = "RSI mean-reversion run on price-to-book (buy when cheap)"

    def __init__(self) -> None:
        super().__init__(base=RsiMeanRevert(window=14, oversold=35.0), metric="price_to_book")


class _ValGateBollinger(ValuationGateOverlay):
    name = "val_gate_bollinger"
    description = "Price Bollinger entries confirmed only when P/B is cheap"

    def __init__(self) -> None:
        super().__init__(base=BollingerMeanRevert(window=30, n_std=3.0), metric="price_to_book")


class _ValComposite(CompositeStrategy):
    name = "val_composite"
    description = "Union of price Bollinger and valuation-run Bollinger (recall overlap)"

    def __init__(self) -> None:
        super().__init__(
            members=[
                BollingerMeanRevert(window=30, n_std=3.0),
                ValuationStrategy(BollingerMeanRevert(window=30, n_std=3.0), metric="price_to_book"),
            ]
        )


# One entry per registered valuation strategy, keyed ``val_*``.
VALUATION_STRATEGIES: dict[str, type[Strategy]] = {
    "val_bollinger": _ValBollinger,
    "val_rsi": _ValRsi,
    "val_gate_bollinger": _ValGateBollinger,
    "val_composite": _ValComposite,
}
