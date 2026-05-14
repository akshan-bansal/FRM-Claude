"""Pidfile-managed daemon runner for the autonomous trading loop.

Cross-platform start/stop/status. On Windows we use ``subprocess.Popen`` with
``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` so the spawned process survives
the launcher exiting. On POSIX we use ``os.setsid`` via ``start_new_session``.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


PIDFILE_NAME = "autonomous.pid"
STDOUT_LOG = "autonomous.out.log"
STDERR_LOG = "autonomous.err.log"


@dataclass(frozen=True)
class DaemonStatus:
    running: bool
    pid: int | None
    pidfile: Path
    stale: bool


class AutonomousDaemon:
    def __init__(self, state_dir: Path, log_dir: Path) -> None:
        self.state_dir = state_dir
        self.log_dir = log_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    @property
    def pidfile(self) -> Path:
        return self.state_dir / PIDFILE_NAME

    @property
    def stdout_log(self) -> Path:
        return self.log_dir / STDOUT_LOG

    @property
    def stderr_log(self) -> Path:
        return self.log_dir / STDERR_LOG

    def status(self) -> DaemonStatus:
        if not self.pidfile.exists():
            return DaemonStatus(running=False, pid=None, pidfile=self.pidfile, stale=False)
        try:
            pid = int(self.pidfile.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return DaemonStatus(running=False, pid=None, pidfile=self.pidfile, stale=True)
        if not _pid_alive(pid):
            return DaemonStatus(running=False, pid=pid, pidfile=self.pidfile, stale=True)
        return DaemonStatus(running=True, pid=pid, pidfile=self.pidfile, stale=False)

    def start(self, argv: list[str], extra_env: dict[str, str] | None = None) -> int:
        st = self.status()
        if st.running:
            raise RuntimeError(f"Daemon already running (pid={st.pid}). Stop it before starting a new one.")
        if st.stale:
            self.pidfile.unlink(missing_ok=True)

        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        stdout = self.stdout_log.open("ab")
        stderr = self.stderr_log.open("ab")
        kwargs: dict[str, object] = {
            "stdout": stdout,
            "stderr": stderr,
            "stdin": subprocess.DEVNULL,
            "env": env,
            "cwd": str(Path.cwd()),
            "close_fds": True,
        }
        if sys.platform == "win32":
            DETACHED = 0x00000008
            CREATE_NEW = 0x00000200
            kwargs["creationflags"] = DETACHED | CREATE_NEW
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(argv, **kwargs)
        self.pidfile.write_text(str(proc.pid), encoding="utf-8")
        # Give the process a moment to crash early so status() reports correctly.
        time.sleep(0.5)
        if not _pid_alive(proc.pid):
            self.pidfile.unlink(missing_ok=True)
            raise RuntimeError(
                f"Daemon failed to start. Check {self.stderr_log}."
            )
        return proc.pid

    def stop(self, timeout_seconds: float = 10.0) -> bool:
        st = self.status()
        if not st.running or st.pid is None:
            if st.stale:
                self.pidfile.unlink(missing_ok=True)
            return False
        try:
            if sys.platform == "win32":
                # Send CTRL_BREAK to the process group, then terminate as fallback.
                subprocess.run(["taskkill", "/PID", str(st.pid), "/T", "/F"], check=False)
            else:
                os.kill(st.pid, signal.SIGTERM)
        except OSError:
            pass
        # Wait for it to actually die.
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if not _pid_alive(st.pid):
                self.pidfile.unlink(missing_ok=True)
                return True
            time.sleep(0.2)
        return False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return str(pid) in out.stdout
        except (subprocess.SubprocessError, OSError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
