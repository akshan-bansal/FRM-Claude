"""Signal post-processing + the lookahead-bias regression check.

The Strategy classes do the strategy-specific work. This module owns the
generic invariants:
  * signals are shifted by 1 bar before they become positions ("trade on next open")
  * size_hint is a non-negative scaler in [0, 1] of strategy conviction
  * a regression test can validate that no signal value depends on a future close
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class SignalSet:
    """A strategy's output. ``entry`` and ``exit`` are bool-like (1/0)."""

    df: pd.DataFrame  # must contain: close, entry, exit, optionally size_hint

    def to_positions(self) -> pd.Series:
        """Materialize a position track from entry/exit signals.

        BUY on entry==1 and currently flat; CLOSE on exit==1 and currently long.
        Signals are shifted by 1 bar so we trade on the open of the *next* bar.
        """
        entry = self.df["entry"].shift(1).fillna(0).astype(int)
        exit_ = self.df["exit"].shift(1).fillna(0).astype(int)
        position = pd.Series(0, index=self.df.index, dtype=int)
        in_market = 0
        out = []
        for e, x in zip(entry.values, exit_.values, strict=False):
            if not in_market and e == 1:
                in_market = 1
            elif in_market and x == 1:
                in_market = 0
            out.append(in_market)
        return pd.Series(out, index=self.df.index, name="position")


def no_lookahead_check(df: pd.DataFrame, signal_col: str = "entry") -> bool:
    """Sanity check: shifting close by -1 must NOT improve correlation with the signal.

    Pure heuristic — a positive result is suspicious, not necessarily proof.
    Use as a CI test alongside hand-written strategy regression tests.
    """
    if signal_col not in df.columns or "close" not in df.columns:
        return True
    fut = df["close"].shift(-1)
    cur = df["close"]
    sig = df[signal_col]
    return float(sig.corr(fut)) <= float(sig.corr(cur)) + 1e-6
