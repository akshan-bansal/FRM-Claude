"""Questrade REST client.

OAuth refresh-token flow per https://www.questrade.com/api/documentation/authorization.
Refresh tokens are one-shot; we always atomically persist the new one returned
by the token endpoint to ``state/tokens.json.enc`` before doing anything else.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..logging_setup import get_logger
from .base import Broker, BrokerError, OrderRejected, TokenExpired
from .models import Account, Candle, Order, Position, Quote
from .token_store import TokenSet, TokenStore

LOGIN_HOST = "https://login.questrade.com"
TOKEN_PATH = "/oauth2/token"
API_VERSION = "v1"

log = get_logger(__name__)


class QuestradeBroker(Broker):
    """Live broker for Questrade.

    On first call (no saved tokens) we exchange ``initial_refresh_token`` for an
    access token / new refresh token and persist. Subsequent calls reuse the
    cached access token until it expires (~30 min), then auto-refresh.
    """

    name = "questrade"

    def __init__(
        self,
        initial_refresh_token: str,
        token_store: TokenStore,
        timeout: float = 15.0,
    ) -> None:
        self._initial_refresh = initial_refresh_token
        self._store = token_store
        self._timeout = timeout
        self._tokens: TokenSet | None = self._store.load()
        self._client = httpx.Client(timeout=timeout)
        self._symbol_id_cache: dict[str, int] = {}

    # ----- token management -----------------------------------------------

    def _refresh(self, refresh_token: str) -> TokenSet:
        log.info("questrade.token.refresh.start")
        url = f"{LOGIN_HOST}{TOKEN_PATH}"
        resp = self._client.post(
            url,
            params={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        if resp.status_code != 200:
            raise TokenExpired(
                f"Refresh failed ({resp.status_code}): {resp.text[:200]}. "
                "Generate a new refresh token at https://login.questrade.com/APIAccess/UserApps.aspx"
            )
        data = resp.json()
        tokens = TokenSet(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            api_server=data["api_server"].rstrip("/") + "/",
            expires_at_epoch=time.time() + max(int(data.get("expires_in", 1500)) - 60, 60),
            token_type=data.get("token_type", "Bearer"),
        )
        self._store.save(tokens)
        log.info("questrade.token.refresh.ok", api_server=tokens.api_server)
        return tokens

    def _ensure_token(self) -> TokenSet:
        if self._tokens is None:
            if not self._initial_refresh:
                raise BrokerError(
                    "No saved tokens and no initial refresh token. Set QUESTRADE_REFRESH_TOKEN."
                )
            self._tokens = self._refresh(self._initial_refresh)
        elif time.time() >= self._tokens.expires_at_epoch:
            self._tokens = self._refresh(self._tokens.refresh_token)
        return self._tokens

    # ----- HTTP plumbing ---------------------------------------------------

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
        reraise=True,
    )
    def _request(self, method: str, endpoint: str, **kwargs: Any) -> httpx.Response:
        tokens = self._ensure_token()
        url = f"{tokens.api_server}{API_VERSION}/{endpoint.lstrip('/')}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"{tokens.token_type} {tokens.access_token}"
        resp = self._client.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 401:
            # Access token rejected -- try one forced refresh, then re-issue.
            log.warning("questrade.access_token.rejected.retry")
            self._tokens = self._refresh(tokens.refresh_token)
            headers["Authorization"] = f"{self._tokens.token_type} {self._tokens.access_token}"
            resp = self._client.request(method, url, headers=headers, **kwargs)
        if resp.status_code >= 400:
            raise BrokerError(f"{method} {endpoint} -> {resp.status_code}: {resp.text[:400]}")
        return resp

    # ----- public surface --------------------------------------------------

    def accounts(self) -> list[Account]:
        data = self._request("GET", "accounts").json()
        return [Account(**a) for a in data.get("accounts", [])]

    def positions(self, account_number: str) -> list[Position]:
        data = self._request("GET", f"accounts/{account_number}/positions").json()
        return [Position(**p) for p in data.get("positions", [])]

    def equity(self, account_number: str) -> float:
        data = self._request("GET", f"accounts/{account_number}/balances").json()
        for bal in data.get("combinedBalances", []):
            if bal.get("currency") == "USD":
                return float(bal.get("totalEquity", 0.0))
        # fall back to first combined balance
        if data.get("combinedBalances"):
            return float(data["combinedBalances"][0].get("totalEquity", 0.0))
        return 0.0

    def _symbol_id(self, symbol: str) -> int:
        if symbol in self._symbol_id_cache:
            return self._symbol_id_cache[symbol]
        data = self._request("GET", "symbols/search", params={"prefix": symbol}).json()
        for s in data.get("symbols", []):
            if s.get("symbol", "").upper() == symbol.upper():
                sid = int(s["symbolId"])
                self._symbol_id_cache[symbol] = sid
                return sid
        raise BrokerError(f"Symbol not found: {symbol}")

    def quote(self, symbol: str) -> Quote:
        sid = self._symbol_id(symbol)
        data = self._request("GET", "markets/quotes", params={"ids": sid}).json()
        rows = data.get("quotes") or []
        if not rows:
            raise BrokerError(f"No quote returned for {symbol}")
        return Quote(**rows[0])

    def quotes(self, symbols: list[str]) -> list[Quote]:
        if not symbols:
            return []
        ids = [str(self._symbol_id(s)) for s in symbols]
        data = self._request("GET", "markets/quotes", params={"ids": ",".join(ids)}).json()
        return [Quote(**q) for q in data.get("quotes", [])]

    def candles(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        interval: str = "OneDay",
    ) -> list[Candle]:
        sid = self._symbol_id(symbol)
        params = {
            "startTime": start.isoformat(),
            "endTime": end.isoformat(),
            "interval": interval,
        }
        data = self._request("GET", f"markets/candles/{sid}", params=params).json()
        return [Candle(**c) for c in data.get("candles", [])]

    def place_order(self, order: Order) -> Order:
        if order.accountId is None:
            raise OrderRejected("order.accountId is required for live submission")
        if order.symbolId is None:
            order.symbolId = self._symbol_id(order.symbol)
        body = order.to_questrade_payload()
        resp = self._request("POST", f"accounts/{order.accountId}/orders", json=body)
        payload = resp.json()
        rejected = (payload.get("orders") or [{}])[0].get("rejectReason")
        if rejected:
            raise OrderRejected(rejected)
        placed = (payload.get("orders") or [{}])[0]
        if "id" in placed:
            order.id = int(placed["id"])
        log.info(
            "questrade.order.placed",
            order_id=order.id,
            symbol=order.symbol,
            qty=order.totalQuantity,
            action=order.action.value,
        )
        return order

    def cancel_order(self, account_number: str, order_id: int) -> None:
        self._request("DELETE", f"accounts/{account_number}/orders/{order_id}")
        log.info("questrade.order.cancelled", order_id=order_id)

    # ----- helpers ---------------------------------------------------------

    @classmethod
    def from_settings(cls, *, refresh_token: str, encryption_key: str, state_dir: Path) -> "QuestradeBroker":
        store = TokenStore(state_dir / "tokens.json.enc", encryption_key)
        return cls(initial_refresh_token=refresh_token, token_store=store)
