"""Overbought exit — exit Variant #3: sell a winner when price is stretched and likely to revert.

The mean-reversion strategies *enter* when price is stretched low and exit when it reverts to the
mean. This is the mirror, applied to every strategy including momentum: close a long that is **in
profit** when the last completed bar shows the stretch a mean-reversion trader would sell into:

* close above the upper Bollinger band (``bb_window``, ``bb_std``), or
* RSI(``rsi_window``) at or above ``rsi_level``.

Same timing and gating as Variant #2 (:mod:`signals.candle_exit`): the signal is read on a
completed bar and acted on the next one; it arms only once the position is up ``min_gain_atr`` ATRs
and the gain covers ``cost_multiple`` round-trip costs; the re-entry lockout applies afterwards.
Indicators are rolling and past-only. RSI warm-up NaNs never fire; a run with no down days (where
``rsi()`` is NaN because the average loss is 0) is scored as RSI 100.

Parameters are the textbook defaults (20/2 sd, 14/70), fixed a priori on 2026-09-18, not fitted.
Off by default everywhere.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .candle_exit import WinnerGate
from .indicators import bollinger, rsi


@dataclass(frozen=True)
class OverboughtExit(WinnerGate):
    bb_window: int = 20
    bb_std: float = 2.0
    rsi_window: int = 14
    rsi_level: float = 70.0

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.bb_window < 2 or self.rsi_window < 2:
            raise ValueError("bb_window and rsi_window must be >= 2")
        if not (50.0 < self.rsi_level < 100.0):
            raise ValueError(f"rsi_level must be in (50, 100), got {self.rsi_level}")

    def fired(self, df: pd.DataFrame) -> pd.DataFrame:
        """Boolean frame with ``close_above_upper_bb`` and ``rsi_overbought`` columns."""
        if "close" not in df.columns:
            return pd.DataFrame(index=df.index)
        close = df["close"].astype(float)
        upper = bollinger(close, self.bb_window, self.bb_std)["bb_upper"]
        r = rsi(close, self.rsi_window)
        # rsi() is NaN when the average loss is zero: a run with no down days, which is the MOST
        # overbought case, not an unknown one. Score it 100 (still past-only; warm-up stays NaN).
        avg_gain = close.diff().clip(lower=0.0).ewm(alpha=1.0 / self.rsi_window,
                                                    min_periods=self.rsi_window, adjust=False).mean()
        r = r.mask(r.isna() & avg_gain.gt(0), 100.0)
        return pd.DataFrame({
            "close_above_upper_bb": (close > upper).fillna(False).astype(bool),
            "rsi_overbought": (r >= self.rsi_level).fillna(False).astype(bool),
        }, index=df.index)
