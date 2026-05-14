"""Append-only journal of order intents, gate decisions, and fills."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class OrderJournal:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.orders_path = state_dir / "orders.jsonl"
        self.rejected_path = state_dir / "rejected.jsonl"
        self.fills_path = state_dir / "fills.jsonl"

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def _append(self, path: Path, row: dict[str, Any]) -> None:
        row.setdefault("ts", self._now())
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def order_intent(self, row: dict[str, Any]) -> None:
        self._append(self.orders_path, row)

    def rejected(self, row: dict[str, Any]) -> None:
        self._append(self.rejected_path, row)

    def fill(self, row: dict[str, Any]) -> None:
        self._append(self.fills_path, row)
