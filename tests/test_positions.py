from __future__ import annotations

import pandas as pd
import pytest

from trading_live_claude.backtest import BacktestEngine
from trading_live_claude.signals.generator import SignalSet


def _frame(close, entry, exit_, atr=None, short_entry=None, short_exit=None) -> pd.DataFrame:
    d: dict[str, object] = {
        "time": pd.date_range("2022-01-01", periods=len(close), freq="B", tz="UTC"),
        "close": close, "entry": entry, "exit": exit_,
    }
    if atr is not None:
        d["atr"] = atr
    if short_entry is not None:
        d["short_entry"] = short_entry
    if short_exit is not None:
        d["short_exit"] = short_exit
    return pd.DataFrame(d)


def test_long_only_is_backward_compatible() -> None:
    # entry on bar 0, exit on bar 3; signals shift one bar before becoming positions.
    pos = SignalSet(_frame([10] * 6, [1, 0, 0, 0, 0, 0], [0, 0, 0, 1, 0, 0])).to_positions().tolist()
    assert pos == [0, 1, 1, 1, 0, 0]


def test_same_bar_entry_and_exit_opens_no_trade() -> None:
    pos = SignalSet(_frame([10] * 4, [0, 1, 0, 0], [0, 1, 0, 0])).to_positions().tolist()
    assert pos == [0, 0, 0, 0]  # completed round trip → never in market


def test_atr_stop_force_closes_a_losing_long() -> None:
    # Enter at 100 with ATR 5 and a 1x stop → floor at 95; bar 2 prints 94 and stops out.
    f = _frame([100, 100, 94, 90, 90], [1, 0, 0, 0, 0], [0, 0, 0, 0, 0], atr=[5, 5, 5, 5, 5])
    pos = SignalSet(f).to_positions(atr_stop_mult=1.0).tolist()
    assert pos[1] == 1 and pos[2] == 0  # long opened, then stopped out
    # Without the stop it would keep holding.
    assert SignalSet(f).to_positions().tolist()[2] == 1


def test_short_channel_opens_and_covers() -> None:
    f = _frame([100] * 4, [0] * 4, [0] * 4, short_entry=[1, 0, 0, 0], short_exit=[0, 0, 1, 0])
    assert SignalSet(f).to_positions().tolist() == [0, -1, -1, 0]


def test_short_channel_ignored_without_both_columns() -> None:
    # short_entry present but no short_exit → treated as long-only (no accidental shorts).
    f = _frame([100] * 4, [0] * 4, [0] * 4)
    f["short_entry"] = [1, 0, 0, 0]
    assert SignalSet(f).to_positions().tolist() == [0, 0, 0, 0]


def test_ledger_scores_short_pnl_with_correct_sign() -> None:
    engine = BacktestEngine()
    signals = _frame([100, 100, 90, 90], [0] * 4, [0] * 4)
    position = pd.Series([0, -1, -1, 0])
    trades = engine._build_trade_ledger(signals, position)
    assert len(trades) == 1
    # short entered at 100, covered at 90 → +10%.
    assert trades[0].pnl_pct == pytest.approx(0.10)
