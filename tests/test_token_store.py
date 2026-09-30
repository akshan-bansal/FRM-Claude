from __future__ import annotations

import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from trading_live_claude.brokers.token_store import TokenSet, TokenStore, _derive_fernet_key


def _ts(refresh: str = "refresh", access: str = "access") -> TokenSet:
    return TokenSet(
        access_token=access,
        refresh_token=refresh,
        api_server="https://api01.iq.questrade.com/",
        expires_at_epoch=time.time() + 1800,
    )


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "t.enc", "secret-key-32-bytes-long-or-better")
    store.save(_ts(refresh="aaa"))
    loaded = store.load()
    assert loaded is not None
    assert loaded.refresh_token == "aaa"


def test_save_overwrite_keeps_backup(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "t.enc", "k1-secret-bytes-bytes-bytes-bytes-")
    store.save(_ts(refresh="first"))
    store.save(_ts(refresh="second"))
    loaded = store.load()
    assert loaded is not None
    assert loaded.refresh_token == "second"
    assert store.backup_path.exists()


def test_wrong_key_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "t.enc"
    TokenStore(p, "key-one-key-one-key-one-key-one-").save(_ts(refresh="abc"))
    assert TokenStore(p, "key-two-key-two-key-two-key-two-").load() is None


def test_empty_secret_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        TokenStore(tmp_path / "t.enc", "")


def _write_legacy(path: Path, secret: str, tokens: TokenSet) -> None:
    """A file as written before 2026-09-21: bare Fernet token, fixed salt."""
    import json
    path.write_bytes(Fernet(_derive_fernet_key(secret)).encrypt(json.dumps(tokens.to_dict()).encode()))


def test_legacy_file_stays_readable(tmp_path: Path) -> None:
    p = tmp_path / "t.enc"
    _write_legacy(p, "legacy-secret-legacy-secret-legacy", _ts(refresh="old"))
    loaded = TokenStore(p, "legacy-secret-legacy-secret-legacy").load()
    assert loaded is not None and loaded.refresh_token == "old"


def test_next_save_migrates_and_backup_keeps_the_legacy_copy_readable(tmp_path: Path) -> None:
    p = tmp_path / "t.enc"
    key = "legacy-secret-legacy-secret-legacy"
    _write_legacy(p, key, _ts(refresh="old"))
    store = TokenStore(p, key)
    store.save(_ts(refresh="new"))
    assert p.read_bytes().startswith(b"TLC2$")
    assert not store.backup_path.read_bytes().startswith(b"TLC2$")     # legacy copy in .bak
    fresh = TokenStore(p, key)                                          # new process, new salt
    assert fresh.load().refresh_token == "new"                          # type: ignore[union-attr]
    p.unlink()                                                          # main file lost mid-write
    assert fresh.load().refresh_token == "old"                          # type: ignore[union-attr]


def test_salt_is_random_per_file(tmp_path: Path) -> None:
    a, b = tmp_path / "a.enc", tmp_path / "b.enc"
    TokenStore(a, "same-secret-same-secret-same-secret").save(_ts())
    TokenStore(b, "same-secret-same-secret-same-secret").save(_ts())
    assert a.read_bytes()[5:21] != b.read_bytes()[5:21]


def test_wrong_key_on_new_format_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "t.enc"
    TokenStore(p, "key-one-key-one-key-one-key-one-").save(_ts())
    assert TokenStore(p, "key-two-key-two-key-two-key-two-").load() is None
