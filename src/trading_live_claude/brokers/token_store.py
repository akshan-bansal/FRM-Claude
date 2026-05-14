"""Encrypted-at-rest token persistence for Questrade refresh tokens.

Questrade refresh tokens are one-shot: every successful exchange returns a NEW
refresh token. Lose it (or use it twice) and the chain breaks. So we:
  1. Write atomically (tmpfile + replace) on every refresh.
  2. Encrypt with Fernet using TOKEN_ENCRYPTION_KEY (derived via PBKDF2).
  3. Keep a backup of the previous token so a crash mid-write doesn't lose access.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    api_server: str  # e.g. https://api01.iq.questrade.com/
    expires_at_epoch: float
    token_type: str = "Bearer"

    def to_dict(self) -> dict:
        return asdict(self)


def _derive_fernet_key(secret: str) -> bytes:
    """Stretch user secret to a Fernet-shaped key. Salt is fixed; rotate the secret to rotate keys."""
    if not secret:
        raise ValueError("TOKEN_ENCRYPTION_KEY is empty; set it in .env before using the broker.")
    salt = b"trading-live-claude/v1/tokens"
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
    return base64.urlsafe_b64encode(kdf.derive(secret.encode("utf-8")))


class TokenStore:
    """Reads/writes the encrypted token blob.

    `path` defaults to state/tokens.json.enc. A plaintext sibling `tokens.json`
    is intentionally NOT written — even for debugging — to avoid leaving
    refresh tokens on disk in cleartext.
    """

    def __init__(self, path: Path, secret: str) -> None:
        self.path = path
        self.backup_path = path.with_suffix(path.suffix + ".bak")
        self._fernet = Fernet(_derive_fernet_key(secret))

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> TokenSet | None:
        for candidate in (self.path, self.backup_path):
            if not candidate.exists():
                continue
            try:
                blob = candidate.read_bytes()
                raw = self._fernet.decrypt(blob)
                data = json.loads(raw)
                return TokenSet(**data)
            except (InvalidToken, json.JSONDecodeError):
                continue
        return None

    def save(self, tokens: TokenSet) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._fernet.encrypt(json.dumps(tokens.to_dict()).encode("utf-8"))

        # Move current -> backup before writing new file.
        if self.path.exists():
            try:
                self.backup_path.write_bytes(self.path.read_bytes())
            except OSError:
                pass

        # Atomic write: temp file in same dir then replace.
        fd, tmp = tempfile.mkstemp(prefix=".tokens.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
