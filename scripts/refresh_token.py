"""One-shot bootstrap: exchange an initial refresh token from .env into the encrypted token store.

Run once after creating your Questrade developer app to seed `state/tokens.json.enc`.
Subsequent refreshes happen automatically when the broker is used.

Usage:
    uv run python scripts/refresh_token.py
"""
from __future__ import annotations

from trading_live_claude.brokers.questrade import QuestradeBroker
from trading_live_claude.config import get_settings


def main() -> int:
    s = get_settings()
    if not s.questrade_refresh_token:
        print("QUESTRADE_REFRESH_TOKEN missing; set it in .env first.")
        return 2
    if not s.token_encryption_key:
        print("TOKEN_ENCRYPTION_KEY missing; set it in .env (32+ random chars).")
        return 2
    broker = QuestradeBroker.from_settings(
        refresh_token=s.questrade_refresh_token,
        encryption_key=s.token_encryption_key,
        state_dir=s.state_dir,
    )
    accounts = broker.accounts()
    print(f"Token exchange OK. Saw {len(accounts)} account(s): {[a.number for a in accounts]}")
    print(f"Encrypted tokens written to: {s.state_dir / 'tokens.json.enc'}")
    print("You should now blank QUESTRADE_REFRESH_TOKEN in .env for safety (the encrypted store has it).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
