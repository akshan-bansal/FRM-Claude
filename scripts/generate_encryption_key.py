"""Generate a fresh TOKEN_ENCRYPTION_KEY for .env."""
from __future__ import annotations

import secrets


def main() -> None:
    print(secrets.token_urlsafe(48))


if __name__ == "__main__":
    main()
