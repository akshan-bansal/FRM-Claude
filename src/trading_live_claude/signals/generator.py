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

    def to_positions(self, *, atr_stop_mult: float | None = None) -> pd.Series:
        """Materialize a position track in ``{-1, 0, +1}`` from entry/exit signals.

        * **Long** — BUY on ``entry`` while flat, CLOSE on ``exit`` while long.
        * **Short** (optional) — SELL on ``short_entry`` while flat, COVER on ``short_exit``
          while short. Active only when the strategy supplies both ``short_entry`` and
          ``short_exit`` columns; absent them the track is long-only, exactly as before.
        * **ATR stop** (optional) — when ``atr_stop_mult`` is set and an ``atr`` column is
          present, a position is force-closed once price moves ``atr_stop_mult x ATR-at-
          entry`` against it. This is the per-trade downside floor the strategies' own
          mean-reversion exits lack.
        * A bar that fires ``entry`` and ``exit`` together while flat is a completed round
          trip and opens no position (so a single-bar cross can't leave a stuck exit).

        Signals and the ATR are shifted one bar (trade on the next bar; the stop distance is
        known at entry, never from a future bar), so no lookahead is introduced.
        """
        df = self.df
        n = len(df)
        entry = df["entry"].shift(1).fillna(0).astype(int).to_numpy()
        exit_ = df["exit"].shift(1).fillna(0).astype(int).to_numpy()
        has_short = "short_entry" in df.columns and "short_exit" in df.columns
        s_entry = df["short_entry"].shift(1).fillna(0).astype(int).to_numpy() if has_short else None
        s_exit = df["short_exit"].shift(1).fillna(0).astype(int).to_numpy() if has_short else None
        close = df["close"].to_numpy(dtype=float)
        use_stop = atr_stop_mult is not None and atr_stop_mult > 0 and "atr" in df.columns
        atr_arr = df["atr"].shift(1).fillna(0.0).to_numpy(dtype=float) if use_stop else None

        pos = 0
        entry_px = 0.0
        entry_atr = 0.0
        out = [0] * n
        for i in range(n):
            if pos == 1:
                stopped = atr_arr is not None and entry_atr > 0.0 and close[i] <= entry_px - atr_stop_mult * entry_atr  # type: ignore[operator]
                if exit_[i] == 1 or stopped:
                    pos = 0
            elif pos == -1:
                stopped = atr_arr is not None and entry_atr > 0.0 and close[i] >= entry_px + atr_stop_mult * entry_atr  # type: ignore[operator]
                if (s_exit is not None and s_exit[i] == 1) or stopped:
                    pos = 0
            if pos == 0:
                long_sig = entry[i] == 1
                short_sig = s_entry is not None and s_entry[i] == 1
                if long_sig and exit_[i] == 1:
                    pass  # entry+exit on the same bar → completed move, no trade
                elif long_sig:
                    pos, entry_px = 1, close[i]
                    entry_atr = atr_arr[i] if atr_arr is not None else 0.0
                elif short_sig and not (s_exit is not None and s_exit[i] == 1):
                    pos, entry_px = -1, close[i]
                    entry_atr = atr_arr[i] if atr_arr is not None else 0.0
            out[i] = pos
        return pd.Series(out, index=df.index, name="position")


def candidate_strength(df: pd.DataFrame) -> pd.Series:
    """Graded [0, 1] strength for each bar, for the scoring/precision stage.

    Uses the strategy-supplied ``signal_strength`` column when present, otherwise
    falls back to the binary ``entry`` value as a float. Always clipped to [0, 1]
    so downstream scorers can treat it as a bounded feature.
    """
    if "signal_strength" in df.columns:
        s = df["signal_strength"]
    elif "entry" in df.columns:
        s = df["entry"].astype(float)
    else:
        raise ValueError("candidate_strength requires a 'signal_strength' or 'entry' column")
    return s.fillna(0.0).clip(0.0, 1.0).rename("signal_strength")


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
