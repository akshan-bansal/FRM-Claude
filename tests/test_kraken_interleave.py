from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "kraken_interleave", Path(__file__).resolve().parents[1] / "scripts" / "kraken_interleave.py")
assert _SPEC and _SPEC.loader
ki = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ki)


def _checkpoint(tmp_path: Path, pair: str, *, complete: bool) -> None:
    (tmp_path / f"{pair}_history.json").write_text(json.dumps({"complete": complete}),
                                                   encoding="utf-8")


def test_only_a_complete_checkpoint_skips_a_pair(tmp_path: Path) -> None:
    """Keying on the parquet would abandon a pair mid-history: it exists after the first chunk."""
    _checkpoint(tmp_path, "XBTUSD", complete=True)
    _checkpoint(tmp_path, "ETHUSD", complete=False)
    (tmp_path / "ETHUSD_daily.parquet").write_bytes(b"")     # partial data present, not done
    assert ki.needs_history("XBTUSD", tmp_path) is False
    assert ki.needs_history("ETHUSD", tmp_path) is True
    assert ki.needs_history("ZECUSD", tmp_path) is True      # no checkpoint at all


def test_unreadable_checkpoint_resumes_rather_than_skipping(tmp_path: Path) -> None:
    (tmp_path / "XMRUSD_history.json").write_text("{torn", encoding="utf-8")
    assert ki.needs_history("XMRUSD", tmp_path) is True


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

    def _step(script: str, args: list[str], label: str) -> int:
        calls.append(label)
        if script == "fetch_crypto_history.py":       # the real fetch would checkpoint; fake it
            _checkpoint(tmp_path, args[args.index("--pair") + 1], complete=True)
        return 0

    monkeypatch.setattr(ki, "run_step", _step)
    _checkpoint(tmp_path, "ZECUSD", complete=True)                # already done -> history skipped
    monkeypatch.setattr(sys, "argv", ["kraken_interleave.py", "--pairs", "XMRUSD,ZECUSD",
                                      "--cache", str(tmp_path),
                                      "--stop-file", str(tmp_path / "nope")])
    assert ki.main() == 0
    assert calls == ["history XMRUSD run 1/6 (7500 pages)", "tick pass after XMRUSD",
                     "tick pass after ZECUSD"]


def test_an_incomplete_pair_is_re_run_up_to_the_cap(monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path: Path) -> None:
    """A pair bigger than one run's page cap resumes; the cap stops it blocking the queue."""
    calls: list[str] = []
    monkeypatch.setattr(ki, "fetch_running", lambda: False)
    monkeypatch.setattr(ki, "run_step", lambda script, args, label: calls.append(label) or 0)
    monkeypatch.setattr(sys, "argv", ["kraken_interleave.py", "--pairs", "XMRUSD",
                                      "--cache", str(tmp_path), "--history-runs-per-pair", "3",
                                      "--skip-ticks", "--stop-file", str(tmp_path / "nope")])
    assert ki.main() == 0
    assert calls == [f"history XMRUSD run {n}/3 (7500 pages)"
                     for n in (1, 2, 3)]                               # never completes -> capped


def test_skip_ticks_runs_history_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """While a paper session is live the tick passes must not compete for the rate limit."""
    calls: list[str] = []
    monkeypatch.setattr(ki, "fetch_running", lambda: False)
    monkeypatch.setattr(ki, "run_step", lambda script, args, label: calls.append(label) or 0)
    _checkpoint(tmp_path, "XMRUSD", complete=True)
    monkeypatch.setattr(sys, "argv", ["kraken_interleave.py", "--pairs", "XMRUSD",
                                      "--cache", str(tmp_path), "--skip-ticks",
                                      "--stop-file", str(tmp_path / "nope")])
    assert ki.main() == 0
    assert calls == []


def test_deadline_is_the_next_occurrence_of_that_local_time() -> None:
    """A run started at 18:00 must aim at TOMORROW's 08:45, not a time already past."""
    evening = datetime(2026, 9, 23, 18, 0)
    assert ki.parse_deadline("08:45", now=evening) == datetime(2026, 9, 24, 8, 45)
    morning = datetime(2026, 9, 23, 7, 0)
    assert ki.parse_deadline("08:45", now=morning) == datetime(2026, 9, 23, 8, 45)
    assert ki.parse_deadline("", now=evening) is None
    assert ki.parse_deadline("  ", now=evening) is None


def test_page_budget_shrinks_to_fit_the_remaining_time() -> None:
    """The point of --until: never start a run that would still be fetching in market hours."""
    now = datetime(2026, 9, 23, 18, 0)
    far = datetime(2026, 9, 24, 8, 45)                 # 14h45m away: the cap binds
    assert ki.pages_for_run(7500, far, sleep_s=1.05, now=now) == 7500
    near = datetime(2026, 9, 23, 18, 10)               # 600s away: ~571 pages minus margin
    assert ki.pages_for_run(7500, near, sleep_s=1.05, now=now) == int(600 / 1.05) - 30
    assert ki.pages_for_run(7500, now, sleep_s=1.05, now=now) == 0            # deadline reached
    assert ki.pages_for_run(7500, None, sleep_s=1.05, now=now) == 7500        # no deadline


def test_driver_stops_at_the_deadline_instead_of_starting_a_short_run(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(ki, "fetch_running", lambda: False)
    monkeypatch.setattr(ki, "run_step", lambda script, args, label: calls.append(label) or 0)
    monkeypatch.setattr(ki, "pages_for_run", lambda *a, **k: 10)      # below --min-run-pages
    monkeypatch.setattr(sys, "argv", ["kraken_interleave.py", "--pairs", "XMRUSD",
                                      "--cache", str(tmp_path), "--until", "08:45",
                                      "--skip-ticks", "--stop-file", str(tmp_path / "nope")])
    assert ki.main() == 0
    assert calls == []
