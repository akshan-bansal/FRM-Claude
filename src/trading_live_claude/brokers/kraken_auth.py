"""Kraken private REST authentication — request signing and a signed POST helper.

Kraken's private endpoints authenticate with two headers: ``API-Key`` and ``API-Sign``. The
signature is ``HMAC-SHA512(base64_decode(secret), path_bytes + SHA256(nonce + postdata))``,
base64-encoded. This module is the reusable auth core for the future KrakenBroker; it reads no
secrets itself — the caller passes the key/secret from :mod:`...config.settings` (i.e. from ``.env``).

``sign`` is pure and unit-tested against Kraken's documented example vector. ``private_post`` adds
the nonce, signs, and POSTs with ``httpx``. Nothing here places an order on its own — callers do.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx

KRAKEN_REST = "https://api.kraken.com"


class KrakenAuthError(RuntimeError):
    """Kraken returned an ``error`` array (bad key/permissions/nonce, or a request error)."""


def sign(path: str, data: dict[str, object], secret: str) -> str:
    """Return the base64 ``API-Sign`` for a private request.

    ``data`` must already contain the ``nonce``; ``path`` is the URL path (e.g.
    ``/0/private/Balance``); ``secret`` is the base64 API private key.
    """
    postdata = urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def private_post(path: str, *, key: str, secret: str, data: dict[str, object] | None = None,
                 client: httpx.Client | None = None, timeout: float = 30.0) -> dict[str, object]:
    """Signed POST to a Kraken private endpoint; returns the ``result`` or raises ``KrakenAuthError``.

    A fresh millisecond ``nonce`` is added per call. Read-only endpoints (Balance, OpenOrders) need
    only Query permissions on the key; order endpoints need Create & Modify Orders.
    """
    if not key or not secret:
        raise KrakenAuthError(["Missing Kraken API key/secret (set KRAKEN_API_KEY / KRAKEN_API_SECRET in .env)"])
    body: dict[str, object] = dict(data or {})
    body["nonce"] = str(int(time.time() * 1000))
    headers = {"API-Key": key, "API-Sign": sign(path, body, secret), "User-Agent": "FRM-Claude/1.0"}
    owns = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        r = client.post(KRAKEN_REST + path, data=body, headers=headers)
        payload = r.json()
    finally:
        if owns:
            client.close()
    if payload.get("error"):
        raise KrakenAuthError(payload["error"])
    result = payload.get("result", {})
    return result if isinstance(result, dict) else {}
