from __future__ import annotations

import threading
from pathlib import Path

import httpx
import respx

from trading_live_claude.brokers import QuestradeBroker


def _broker(tmp_path: Path) -> QuestradeBroker:
    return QuestradeBroker.from_settings(
        refresh_token="INITIAL_REFRESH",
        encryption_key="test-key-do-not-use-in-production",
        state_dir=tmp_path,
    )


@respx.mock
def test_concurrent_callers_refresh_token_once(tmp_path: Path) -> None:
    """The token lock must collapse a thundering herd into a single refresh.

    Without synchronization, N threads with no cached token each POST the one-shot
    refresh, racing the token-store write and rotating the token out from under each
    other. With the lock, exactly one refresh happens and the rest reuse it.
    """
    token_route = respx.post("https://login.questrade.com/oauth2/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "AT",
                "refresh_token": "ROTATED",
                "api_server": "https://apitest.questrade.com/",
                "expires_in": 1800,
                "token_type": "Bearer",
            },
        )
    )
    accounts_route = respx.get("https://apitest.questrade.com/v1/accounts").mock(
        return_value=httpx.Response(200, json={"accounts": []})
    )

    broker = _broker(tmp_path)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            broker.accounts()
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert token_route.call_count == 1  # herd collapsed to one refresh
    assert accounts_route.called


@respx.mock
def test_401_retry_follows_new_api_server(tmp_path: Path) -> None:
    """A refresh can return a different api_server; the 401 retry must follow it.

    Regression for the monitor crash: an api07 token was retried against api05 (the
    old host), 401ing again. The retry must rebuild the URL from the fresh token.
    """
    respx.post("https://login.questrade.com/oauth2/token").mock(
        side_effect=[
            httpx.Response(200, json={
                "access_token": "AT1", "refresh_token": "R2",
                "api_server": "https://api07.iq.questrade.com/", "expires_in": 1800, "token_type": "Bearer"}),
            httpx.Response(200, json={
                "access_token": "AT2", "refresh_token": "R3",
                "api_server": "https://api05.iq.questrade.com/", "expires_in": 1800, "token_type": "Bearer"}),
        ]
    )
    old = respx.get("https://api07.iq.questrade.com/v1/accounts").mock(
        return_value=httpx.Response(401, json={"code": 1017, "message": "Access token is invalid"})
    )
    new = respx.get("https://api05.iq.questrade.com/v1/accounts").mock(
        return_value=httpx.Response(200, json={"accounts": []})
    )

    assert _broker(tmp_path).accounts() == []
    assert old.called  # first attempt hit the stale host and 401'd
    assert new.called  # retry followed the refresh to the new host
