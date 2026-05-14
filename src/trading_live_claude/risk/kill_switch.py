"""Kill-switch: a single sentinel file blocks every live order.

The sentinel is intentionally a file (not a DB row) so flipping it is
trivial — `touch state/HALTED` from any shell terminates all entries.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class KillState:
    halted: bool
    reason: str
    activated_at: datetime | None


class KillSwitch:
    SENTINEL_NAME = "HALTED"

    def __init__(
        self,
        state_dir: Path,
        max_drawdown_pct: float = 0.10,
        daily_loss_limit_pct: float = 0.03,
    ) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.max_drawdown_pct = max_drawdown_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct

    @property
    def _path(self) -> Path:
        return self.state_dir / self.SENTINEL_NAME

    def state(self) -> KillState:
        if not self._path.exists():
            return KillState(halted=False, reason="", activated_at=None)
        try:
            content = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            content = ""
        return KillState(
            halted=True,
            reason=content or "unknown",
            activated_at=datetime.fromtimestamp(self._path.stat().st_mtime, tz=UTC),
        )

    def trip(self, reason: str) -> None:
        self._path.write_text(f"{datetime.now(UTC).isoformat()} :: {reason}\n", encoding="utf-8")
        log.error("kill_switch.tripped", reason=reason)

    def clear(self, ack: str) -> None:
        """Remove the sentinel. Requires explicit ack text to discourage accidental clears."""
        if ack.strip().upper() != "I HAVE INVESTIGATED":
            raise PermissionError("Refusing to clear kill switch without explicit ack.")
        if self._path.exists():
            self._path.unlink()
            log.warning("kill_switch.cleared")

    def evaluate(self, *, equity: float, peak_equity: float, day_open_equity: float) -> KillState:
        """Decide whether to trip based on current account state."""
        if equity <= 0:
            self.trip("equity <= 0")
            return self.state()
        dd = 1.0 - (equity / peak_equity) if peak_equity > 0 else 0.0
        if dd >= self.max_drawdown_pct:
            self.trip(f"max-drawdown {dd:.2%} >= {self.max_drawdown_pct:.2%}")
            return self.state()
        daily = 1.0 - (equity / day_open_equity) if day_open_equity > 0 else 0.0
        if daily >= self.daily_loss_limit_pct:
            self.trip(f"daily-loss {daily:.2%} >= {self.daily_loss_limit_pct:.2%}")
            return self.state()
        return self.state()
