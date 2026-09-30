"""Tests for data/cache.py — the parquet candle cache (added 2026-09-18 with the atomic write)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from trading_live_claude.data.cache import CandleCache

S, E = datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 6, 1, tzinfo=UTC)


def _df() -> pd.DataFrame:
    return pd.DataFrame({"time": pd.date_range("2024-01-01", periods=3, tz="UTC"),
                         "close": [1.0, 2.0, 3.0]})


def test_round_trip_and_miss(tmp_path: Path) -> None:
    c = CandleCache(tmp_path)
    assert c.get("XIC.TO", "1d", S, E) is None
    c.put("XIC.TO", "1d", S, E, _df())
    got = c.get("XIC.TO", "1d", S, E)
    assert got is not None and got["close"].tolist() == [1.0, 2.0, 3.0]


def test_put_leaves_no_temp_files_and_overwrites_in_place(tmp_path: Path) -> None:
    c = CandleCache(tmp_path)
    c.put("QQQ", "1d", S, E, _df())
    c.put("QQQ", "1d", S, E, _df().assign(close=[4.0, 5.0, 6.0]))
    assert len(list(tmp_path.glob("*.parquet"))) == 1 and not list(tmp_path.glob("*.tmp"))
    assert c.get("QQQ", "1d", S, E)["close"].tolist() == [4.0, 5.0, 6.0]


def test_failed_write_keeps_the_previous_file_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = CandleCache(tmp_path)
    c.put("VDY.TO", "1d", S, E, _df())

    def boom(self, path, **kw):   # simulates a crash mid-write of the temp file
        Path(path).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", boom)
    c.put("VDY.TO", "1d", S, E, _df().assign(close=[9.0, 9.0, 9.0]))
    monkeypatch.undo()
    assert c.get("VDY.TO", "1d", S, E)["close"].tolist() == [1.0, 2.0, 3.0]
    assert not list(tmp_path.glob("*.tmp"))
