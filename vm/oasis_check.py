"""Did the simulated agents actually DO anything? Standard library only; reads, never writes.

    python oasis_check.py <oasis_RUNID.db> [log_dir]

OASIS swallows every model error and writes it to ``log/social.agent-*.log``, so a run in which every
LLM call failed still "completes": the agents sign up and refresh, the seed post exists, and nothing
else happens. That is exactly what the 2026-10-03 database held. This prints the action counts and,
when the agents did nothing, the first logged errors, so the cause is on screen instead of buried.

Exit code: 0 when at least one agent acted beyond the seed post, 1 when none did, 2 on a usage error.
"""
from __future__ import annotations

import glob
import re
import sqlite3
import sys
from pathlib import Path

PASSIVE = {"sign_up", "refresh"}          # logged for every agent whether or not the model did anything
MAX_ERRORS = 8


def action_counts(db: Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return {str(a): int(n) for a, n in con.execute("SELECT action, COUNT(*) FROM trace GROUP BY action")}
    finally:
        con.close()


def agent_actions(counts: dict[str, int]) -> int:
    """Actions the MODEL took. The seed post is made by the runner (a ManualAction), so one
    ``create_post`` is not the society's."""
    total = sum(n for a, n in counts.items() if a not in PASSIVE)
    return max(0, total - (1 if counts.get("create_post", 0) >= 1 else 0))


def logged_errors(log_dir: Path) -> list[str]:
    seen: list[str] = []
    for f in sorted(glob.glob(str(log_dir / "social.agent-*.log"))):
        for line in Path(f).read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("ERROR"):
                msg = re.sub(r"^ERROR - [\d\- :,]+ - social\.agent - ", "", line)[:300]
                if msg not in seen:
                    seen.append(msg)
    return seen


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python oasis_check.py <oasis_RUNID.db> [log_dir]")
        return 2
    db = Path(argv[1])
    log_dir = Path(argv[2]) if len(argv) > 2 else db.parent / "log"
    if not db.exists():
        print(f"No database at {db}. The run did not get as far as creating one.")
        return 2
    counts = action_counts(db)
    acted = agent_actions(counts)
    print("Actions recorded:", ", ".join(f"{a}={n}" for a, n in sorted(counts.items())) or "none")
    if acted > 0:
        print(f"OK: the agents took {acted} action(s) beyond the seed post.")
        return 0
    print("THE AGENTS DID NOTHING beyond the seed post. OASIS hides model errors in its log:")
    if not log_dir.is_dir():
        print(f"  no log folder at {log_dir}")
        return 1
    errs = logged_errors(log_dir)
    if not errs:
        print(f"  no ERROR lines in {log_dir} (look at the INFO lines there)")
    for e in errs[:MAX_ERRORS]:
        print("  -", e)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
