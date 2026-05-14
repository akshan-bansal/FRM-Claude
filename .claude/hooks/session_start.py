"""SessionStart hook: auto-start autonomous daemon if enabled in .env.

Hook is wired in `.claude/settings.json` under `hooks.SessionStart`. It runs
once per Claude Code session boot in this repo. Idempotent: if the daemon
is already running, this is a no-op.

Behavior:
  * Reads .env via pydantic Settings.
  * If AUTONOMOUS_ENABLED=false or AUTO_START_ON_SESSION=false: exits 0.
  * If kill-switch tripped: exits 0 with a notice (does NOT clear it).
  * Otherwise: spawns the daemon and exits.

Exit code is always 0 unless catastrophic; we never want the hook to block
the Claude session from starting.
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    try:
        from trading_live_claude.config import get_settings
        from trading_live_claude.daemon import AutonomousDaemon
        from trading_live_claude.execution import AUTONOMOUS_ENV_VAR
        from trading_live_claude.risk import KillSwitch
    except Exception as e:  # pragma: no cover
        print(f"[session_start] import failed (likely first-time setup); skipping: {e}", file=sys.stderr)
        return 0

    settings = get_settings()
    if not settings.autonomous_enabled:
        print("[session_start] AUTONOMOUS_ENABLED=false; not auto-starting daemon.")
        return 0
    if not settings.autonomous_auto_start_on_session:
        print("[session_start] autonomous_auto_start_on_session=false; not auto-starting daemon.")
        return 0

    ks = KillSwitch(settings.state_dir).state()
    if ks.halted:
        print(f"[session_start] kill-switch tripped ({ks.reason}); not auto-starting daemon.")
        return 0

    daemon = AutonomousDaemon(settings.state_dir, settings.log_dir)
    st = daemon.status()
    if st.running:
        print(f"[session_start] daemon already running (pid={st.pid}); no action.")
        return 0

    argv = [sys.executable, "-m", "trading_live_claude.cli", "autonomous", "run"]
    extra_env = {
        AUTONOMOUS_ENV_VAR: "true",
        "AUTONOMOUS_ACCOUNT_RUNTIME": settings.autonomous_account,
    }
    try:
        pid = daemon.start(argv, extra_env=extra_env)
        print(
            f"[session_start] started autonomous daemon "
            f"(pid={pid}, account={settings.autonomous_account}, "
            f"strategy={settings.autonomous_strategy}, "
            f"interval={settings.autonomous_interval_seconds}s)."
        )
    except RuntimeError as e:
        print(f"[session_start] daemon failed to start: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
