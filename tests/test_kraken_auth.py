from __future__ import annotations

import httpx
import pytest
import respx

from trading_live_claude.brokers.kraken_auth import KrakenAuthError, private_post, sign

# Kraken's documented signing example (REST authentication docs).
_SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="


def test_sign_matches_kraken_documented_vector() -> None:
    data = {"nonce": "1616492376594", "ordertype": "limit", "pair": "XBTUSD",
            "price": 37500, "type": "buy", "volume": 1.25}
    sig = sign("/0/private/AddOrder", data, _SECRET)
    assert sig == "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="


def test_private_post_requires_credentials() -> None:
    with pytest.raises(KrakenAuthError, match="Missing"):
        private_post("/0/private/Balance", key="", secret="")


@respx.mock
def test_private_post_returns_result_and_signs() -> None:
    route = respx.post("https://api.kraken.com/0/private/Balance").mock(
        return_value=httpx.Response(200, json={"error": [], "result": {"ZUSD": "100.0", "XXBT": "0.5"}})
    )
    out = private_post("/0/private/Balance", key="k", secret=_SECRET)
    assert out == {"ZUSD": "100.0", "XXBT": "0.5"}
    sent = route.calls.last.request
    assert sent.headers["API-Key"] == "k" and sent.headers["API-Sign"]  # signed headers present


@respx.mock
def test_private_post_raises_on_kraken_error() -> None:
    respx.post("https://api.kraken.com/0/private/Balance").mock(
        return_value=httpx.Response(200, json={"error": ["EAPI:Invalid key"], "result": {}})
    )
    with pytest.raises(KrakenAuthError, match="Invalid key"):
        private_post("/0/private/Balance", key="bad", secret=_SECRET)
