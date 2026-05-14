from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from trading_live_claude.daemon import AutonomousDaemon


def test_status_with_no_pidfile(tmp_path: Path) -> None:
    d = AutonomousDaemon(tmp_path / "state", tmp_path / "logs")
    st = d.status()
    assert not st.running
    assert st.pid is None
    assert not st.stale


def test_status_with_stale_pidfile(tmp_path: Path) -> None:
    state = tmp_path / "daemon-state-a"
    state.mkdir(parents=True, exist_ok=True)
    (state / "autonomous.pid").write_text("99999999", encoding="utf-8")
    d = AutonomousDaemon(state, tmp_path / "logs-a")
    st = d.status()
    assert not st.running
    assert st.stale


def test_status_running_for_own_pid(tmp_path: Path) -> None:
    state = tmp_path / "daemon-state-b"
    state.mkdir(parents=True, exist_ok=True)
    (state / "autonomous.pid").write_text(str(os.getpid()), encoding="utf-8")
    d = AutonomousDaemon(state, tmp_path / "logs-b")
    st = d.status()
    assert st.running
    assert st.pid == os.getpid()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only signal semantics test")
def test_start_and_stop_short_lived(tmp_path: Path) -> None:
    d = AutonomousDaemon(tmp_path / "state", tmp_path / "logs")
    pid = d.start([sys.executable, "-c", "import time;time.sleep(30)"])
    assert pid > 0
    assert d.status().running
    assert d.stop(timeout_seconds=5)
    assert not d.status().running
