"""Signal post-processing + the lookahead-bias regression check.

The Strategy classes do the strategy-specific work. This module owns the
generic invariants:
  * signals are shifted by 1 bar before they become positions ("trade on next open")
  * size_hint is a non-negative scaler in [0, 1] of strategy conviction
  * a regression test can validate that no signal value depends on a future close
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .candle_exit import CandleExit, WinnerGate
from .indicators import atr as _atr
from .overbought_exit import OverboughtExit
from .profit_lock import ProfitLock


@dataclass
class SignalSet:
    """A strategy's output. ``entry`` and ``exit`` are bool-like (1/0)."""

    df: pd.DataFrame  # must contain: close, entry, exit, optionally size_hint

    def to_positions(
        self,
        *,
        atr_stop_mult: float | None = None,
        trail_atr_mult: float | None = None,
        time_stop_bars: int | None = None,
        profit_lock: ProfitLock | None = None,
        round_trip_cost_frac: float = 0.0,
        candle_exit: CandleExit | None = None,
        overbought_exit: OverboughtExit | None = None,
    ) -> pd.Series:
        """Materialize a position track in ``{-1, 0, +1}`` from entry/exit signals.

        * **Long** — BUY on ``entry`` while flat, CLOSE on ``exit`` while long.
        * **Short** (optional) — SELL on ``short_entry`` while flat, COVER on ``short_exit``
          while short. Active only when the strategy supplies both ``short_entry`` and
          ``short_exit`` columns; absent them the track is long-only, exactly as before.
        * **Fixed ATR stop** (``atr_stop_mult``) — force-close once price moves
          ``atr_stop_mult x ATR-at-entry`` against the entry.
        * **Chandelier trailing stop** (``trail_atr_mult``) — force-close once price
          retraces ``trail_atr_mult x ATR`` from the best close reached since entry. The
          stop ratchets toward profit and never loosens, so it lets a winner run instead of
          capping it — the right stop for trend/momentum.
        * **Time stop** (``time_stop_bars``) — force-close after N bars in the trade,
          regardless of price. The right stop for mean-reversion: if the thesis hasn't paid
          in N bars it is stale, and it avoids bailing on a dip that was about to revert.
        * **Profit lock** (``profit_lock``, 2026-09-18) — once a position is up ``arm_atr`` ATRs,
          force-close when price retraces from its best close by more than an allowance that
          shrinks as the gain grows (see :mod:`signals.profit_lock`). Measured in ATR-at-entry
          units. If the frame has no ``atr`` column it is computed (Wilder, 14) from
          high/low/close. ``round_trip_cost_frac`` turns on its transaction-cost cross-check
          (net-breakeven floor, arms only once the gain covers ``cost_multiple`` round trips).
          After a lock exit, that side cannot re-enter until its entry signal has switched off
          for at least one bar, so a level-style entry cannot buy straight back in.
        * **Candle exit** (``candle_exit``, 2026-09-18, exit Variant #2) — close a long that is
          up ``min_gain_atr`` ATRs (and ``cost_multiple`` round-trip costs) when a bearish
          reversal pattern completed on the previous bar (see :mod:`signals.candle_exit`). Needs
          OHLC in the frame. Shares the re-entry lockout with the profit lock.
        * **Overbought exit** (``overbought_exit``, exit Variant #3) — same timing and gate as the
          candle exit, triggered by a close above the upper Bollinger band or RSI >= 70 on the
          previous bar (see :mod:`signals.overbought_exit`).
        * A bar that fires ``entry`` and ``exit`` together while flat is a completed round
          trip and opens no position.

        Signals and the ATR are shifted one bar (trade on the next bar; stop distances use
        only past/entry information), so no lookahead is introduced.
        """
        df = self.df
        n = len(df)
        entry = df["entry"].shift(1).fillna(0).astype(int).to_numpy()
        exit_ = df["exit"].shift(1).fillna(0).astype(int).to_numpy()
        has_short = "short_entry" in df.columns and "short_exit" in df.columns
        s_entry = df["short_entry"].shift(1).fillna(0).astype(int).to_numpy() if has_short else None
        s_exit = df["short_exit"].shift(1).fillna(0).astype(int).to_numpy() if has_short else None
        close = df["close"].to_numpy(dtype=float)
        wants_atr = ((atr_stop_mult is not None and atr_stop_mult > 0)
                     or (trail_atr_mult is not None and trail_atr_mult > 0) or profit_lock is not None
                     or candle_exit is not None or overbought_exit is not None)
        atr_src: pd.Series | None = None
        if wants_atr and "atr" in df.columns:
            atr_src = df["atr"]
        elif ((profit_lock is not None or candle_exit is not None or overbought_exit is not None)
              and {"high", "low"}.issubset(df.columns)):
            atr_src = _atr(df, 14)
        atr_arr = atr_src.shift(1).fillna(0.0).to_numpy(dtype=float) if atr_src is not None else None
        # Completed-bar signal exits (#2 candle, #3 overbought): signal on the previous bar -> act
        # on this bar (trade-on-next-bar), each with its own winner gate.
        signal_exits: list[tuple[WinnerGate, np.ndarray]] = [
            (sx, sx.any_fired(df).shift(1).fillna(False).to_numpy(dtype=bool))
            for sx in (candle_exit, overbought_exit) if sx is not None
        ]

        pos = 0
        entry_px = 0.0
        entry_atr = 0.0
        extreme = 0.0  # best close since entry: peak for a long, trough for a short
        bars_held = 0
        # Re-entry lockout after a profit-lock exit: +1/-1 blocks that side until its entry signal
        # has been off for a bar. Without it a level-style entry (ts_momentum's ``entry`` is 1 on
        # ~70% of bars) re-buys on the very bar the lock sold, silently undoing the lock here and
        # paying a round trip per poll live.
        lockout = 0
        out = [0] * n
        for i in range(n):
            if pos != 0:
                bars_held += 1
                extreme = max(extreme, close[i]) if pos == 1 else min(extreme, close[i])
                stop_hit = False
                if pos == 1:
                    if atr_stop_mult is not None and entry_atr > 0.0 and close[i] <= entry_px - atr_stop_mult * entry_atr:
                        stop_hit = True
                    if trail_atr_mult is not None and atr_arr is not None and close[i] <= extreme - trail_atr_mult * atr_arr[i]:
                        stop_hit = True
                else:
                    if atr_stop_mult is not None and entry_atr > 0.0 and close[i] >= entry_px + atr_stop_mult * entry_atr:
                        stop_hit = True
                    if trail_atr_mult is not None and atr_arr is not None and close[i] >= extreme + trail_atr_mult * atr_arr[i]:
                        stop_hit = True
                if profit_lock is not None and profit_lock.hit(
                        side=pos, price=close[i], entry=entry_px, extreme=extreme, atr=entry_atr,
                        round_trip_cost_frac=round_trip_cost_frac):
                    stop_hit = True
                    lockout = pos
                if pos == 1 and i > 0:
                    for sx, prev_fired in signal_exits:
                        if prev_fired[i] and sx.armed(entry=entry_px, ref_price=close[i - 1],
                                                      atr=entry_atr,
                                                      round_trip_cost_frac=round_trip_cost_frac):
                            stop_hit = True
                            lockout = pos
                            break
                if time_stop_bars is not None and bars_held >= time_stop_bars:
                    stop_hit = True
                base_exit = exit_[i] == 1 if pos == 1 else (s_exit is not None and s_exit[i] == 1)
                if base_exit or stop_hit:
                    pos = 0
            if pos == 0:
                long_sig = entry[i] == 1
                short_sig = s_entry is not None and s_entry[i] == 1
                if lockout == 1:
                    if long_sig:
                        long_sig = False            # still the same signal the lock sold into
                    else:
                        lockout = 0                 # signal reset: the next entry is a fresh one
                elif lockout == -1:
                    if short_sig:
                        short_sig = False
                    else:
                        lockout = 0
                if long_sig and exit_[i] == 1:
                    pass  # entry+exit on the same bar → completed move, no trade
                elif long_sig or (short_sig and not (s_exit is not None and s_exit[i] == 1)):
                    pos = 1 if long_sig else -1
                    entry_px = extreme = close[i]
                    entry_atr = atr_arr[i] if atr_arr is not None else 0.0
                    bars_held = 0
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
