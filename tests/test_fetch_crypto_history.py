from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "fetch_crypto_history",
    Path(__file__).resolve().parents[1] / "scripts" / "fetch_crypto_history.py")
assert _SPEC and _SPEC.loader
fch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fch)

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _trades(start: int, n: int, *, price: float = 100.0) -> pd.DataFrame:
    """n one-per-minute trades starting `start` minutes after T0.

    Price is a function of the absolute minute, so the same minute always carries the same trade —
    matching Kraken, which re-returns an identical row when the cursor lands on it.
    """
    return pd.DataFrame({
        "time": [T0 + timedelta(minutes=start + i) for i in range(n)],
        "price": [price + start + i for i in range(n)],
        "volume": [1.0] * n,
        "side": ["b"] * n,
    })


def test_merge_drops_the_boundary_duplicate() -> None:
    """Kraken's `since` cursor can re-return the cursor trade; counting it twice corrupts volume."""
    first, second = _trades(0, 3), _trades(2, 3)          # one minute of overlap
    merged = fch.merge_trades(first, second)
    assert len(merged) == 5                               # 3 + 3 - 1 duplicate
    assert merged["time"].is_monotonic_increasing
    assert merged["time"].duplicated().sum() == 0


def test_merge_handles_an_empty_or_missing_side() -> None:
    assert fch.merge_trades(None, _trades(0, 2)).shape[0] == 2
    assert fch.merge_trades(_trades(0, 2), pd.DataFrame()).shape[0] == 2
    assert fch.merge_trades(None, pd.DataFrame()).empty


def test_cursor_is_the_last_trades_nanoseconds() -> None:
    t = _trades(0, 3)
    assert fch.cursor_ns(t) == str(pd.Timestamp(t["time"].iloc[-1]).value)


def test_each_chunk_is_checkpointed_so_a_failure_keeps_earlier_chunks(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch,
                                                                     ) -> None:
    """The 2026-09-23 XMRUSD reset lost 2.4h because nothing was written until the end."""
    calls: list[str] = []

    def fake(pair: str, *, since_ns: str, max_pages: int, sleep_s: float, progress=None):
        calls.append(since_ns)
        if len(calls) == 1:
            return _trades(0, 5)
        raise ConnectionResetError("forcibly closed by the remote host")

    monkeypatch.setattr(fch, "kraken_trades_paginated", fake)
    ck = fch.fetch_pair("XMRUSD", tmp_path, since=None, max_pages=10, chunk_pages=1, sleep_s=0.0)

    assert (tmp_path / "XMRUSD_trades.parquet").exists()          # first chunk survived
    assert (tmp_path / "XMRUSD_daily.parquet").exists()
    assert pd.read_parquet(tmp_path / "XMRUSD_trades.parquet").shape[0] == 5
    assert ck["complete"] is False                                 # resumable, not done
    assert "ConnectionResetError" in ck["last_error"]
    saved = json.loads((tmp_path / "XMRUSD_history.json").read_text(encoding="utf-8"))
    assert saved["cursor_ns"] == ck["cursor_ns"]


def test_a_second_run_resumes_from_the_checkpoint_cursor(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Resuming is the whole point: a restart must not refetch hours of history."""
    monkeypatch.setattr(fch, "kraken_trades_paginated",
                        lambda pair, *, since_ns, max_pages, sleep_s, progress=None: _trades(0, 4))
    first = fch.fetch_pair("XMRUSD", tmp_path, since=None, max_pages=1, chunk_pages=1, sleep_s=0.0)

    seen: list[str] = []

    def fake(pair: str, *, since_ns: str, max_pages: int, sleep_s: float, progress=None):
        seen.append(since_ns)
        return _trades(4, 3)                                      # continues after the cursor

    monkeypatch.setattr(fch, "kraken_trades_paginated", fake)
    second = fch.fetch_pair("XMRUSD", tmp_path, since=None, max_pages=1, chunk_pages=1, sleep_s=0.0)

    assert seen == [first["cursor_ns"]]                           # resumed, not restarted at 0
    assert second["trades_total"] == 7                            # 4 cached + 3 new, merged
    assert pd.read_parquet(tmp_path / "XMRUSD_trades.parquet").shape[0] == 7


def test_a_pair_is_complete_when_the_cursor_stops_advancing(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise the driver would re-run a finished pair forever."""
    monkeypatch.setattr(fch, "kraken_trades_paginated",
                        lambda pair, *, since_ns, max_pages, sleep_s, progress=None: _trades(0, 3))
    ck = fch.fetch_pair("ZECUSD", tmp_path, since=None, max_pages=5, chunk_pages=1, sleep_s=0.0)
    # Chunk 2 returns the same trades, so nothing new merges in -> complete, and the loop stops.
    assert ck["complete"] is True
    assert ck["trades_total"] == 3


def test_empty_first_response_marks_complete_without_writing(tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fch, "kraken_trades_paginated",
                        lambda pair, *, since_ns, max_pages, sleep_s, progress=None: pd.DataFrame())
    fch.fetch_pair("UNIUSD", tmp_path, since=None, max_pages=3, chunk_pages=1, sleep_s=0.0)
    assert not (tmp_path / "UNIUSD_trades.parquet").exists()


def test_unreadable_checkpoint_does_not_crash(tmp_path: Path) -> None:
    (tmp_path / "XBTUSD_history.json").write_text("{not json", encoding="utf-8")
    assert fch.read_checkpoint(tmp_path, "XBTUSD") == {}


def test_atomic_writers_leave_no_tmp_behind(tmp_path: Path) -> None:
    fch.write_json_atomic(tmp_path / "c.json", {"a": 1})
    fch.write_parquet_atomic(_trades(0, 2), tmp_path / "t.parquet")
    assert json.loads((tmp_path / "c.json").read_text(encoding="utf-8")) == {"a": 1}
    assert pd.read_parquet(tmp_path / "t.parquet").shape[0] == 2
    assert list(tmp_path.glob("*.tmp")) == []
