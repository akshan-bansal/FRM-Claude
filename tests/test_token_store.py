from __future__ import annotations

import time
from pathlib import Path

import pytest

from trading_live_claude.brokers.token_store import TokenSet, TokenStore


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
