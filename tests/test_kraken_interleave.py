from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "kraken_interleave", Path(__file__).resolve().parents[1] / "scripts" / "kraken_interleave.py")
assert _SPEC and _SPEC.loader
ki = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ki)


def test_cached_pairs_are_skipped(tmp_path: Path) -> None:
    """A finished pair must not be re-pulled: the fetch overwrites, so a restart would redo hours."""
    (tmp_path / "XBTUSD_daily.parquet").write_bytes(b"")
    assert ki.needs_history("ZECUSD", tmp_path) is True
    assert ki.needs_history("XBTUSD", tmp_path) is False


def test_priority_order_excludes_pairs_that_already_have_daily_bars() -> None:
    """BTC/ETH/PAXG were fetched earlier; re-queuing them would waste hours of rate-limited pulls."""
    assert "XBTUSD" not in ki.DEFAULT_PAIRS
    assert "ETHUSD" not in ki.DEFAULT_PAIRS
    assert "PAXGUSD" not in ki.DEFAULT_PAIRS
    assert ki.DEFAULT_PAIRS[:3] == ("XMRUSD", "ZECUSD", "LINKUSD")


@pytest.mark.parametrize(("stdout", "expected"), [("0", False), ("", False), ("1", True),
                                                  ("2", True), ("  3  ", True)])
def test_fetch_running_reads_the_process_count(monkeypatch: pytest.MonkeyPatch, stdout: str,
                                               expected: bool) -> None:
    """Two concurrent fetches trip Kraken's limiter, so a non-zero count must block the next step."""
    monkeypatch.setattr(ki.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": stdout, "returncode": 0})())
    assert ki.fetch_running() is expected


def test_fetch_running_fails_open_when_the_process_list_is_unreadable(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable process list must not wedge the driver in its wait loop forever."""
    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("no process list")
    monkeypatch.setattr(ki.subprocess, "run", _boom)
    assert ki.fetch_running() is False


def test_stop_file_is_honoured_before_any_step_runs(monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path: Path) -> None:
    """The sentinel must stop the driver between steps — never mid-pair, which loses the pull."""
    calls: list[str] = []
    monkeypatch.setattr(ki, "fetch_running", lambda: False)
    monkeypatch.setattr(ki, "run_step", lambda script, args, label: calls.append(label) or 0)
    stop = tmp_path / "STOP_KRAKEN_FETCH"
    stop.write_text("")
    monkeypatch.setattr(sys, "argv", ["kraken_interleave.py", "--pairs", "ZECUSD",
                                      "--cache", str(tmp_path), "--stop-file", str(stop)])
    assert ki.main() == 0
    assert calls == []


def test_each_history_pair_is_followed_by_a_tick_pass(monkeypatch: pytest.MonkeyPatch,
                                                      tmp_path: Path) -> None:
    """The interleave itself: history, then catch-up, per pair — never two fetches at once."""
    calls: list[str] = []
    monkeypatch.setattr(ki, "fetch_running", lambda: False)
    monkeypatch.setattr(ki, "run_step", lambda script, args, label: calls.append(label) or 0)
    (tmp_path / "ZECUSD_daily.parquet").write_bytes(b"")          # already done -> history skipped
    monkeypatch.setattr(sys, "argv", ["kraken_interleave.py", "--pairs", "XMRUSD,ZECUSD",
                                      "--cache", str(tmp_path),
                                      "--stop-file", str(tmp_path / "nope")])
    assert ki.main() == 0
    assert calls == ["history XMRUSD", "tick pass after XMRUSD", "tick pass after ZECUSD"]
