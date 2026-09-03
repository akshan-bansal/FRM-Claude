"""IBWebBroker — Interactive Brokers Web API (REST) adapter.

Second IB path alongside ``brokers/ib.py``. Where ``IBBroker`` speaks the TWS/IB Gateway
socket API via ``ib_insync``, this one talks to IBKR's **Web API** over HTTPS + JSON — the same
surface behind the Client Portal Gateway. Two reasons to prefer it:

1. **Fits the repo's HTTP convention.** Everything else uses ``httpx``, and tests mock at the
   transport layer with ``respx``. The socket path uses asyncio events with no clean respx
   surface.
2. **Institutional OAuth 2.0 removes the local-process constraint.** Retail accounts still need
   the Client Portal Gateway running locally (a ~50 MB Java daemon that holds a session cookie),
   but institutional accounts can authenticate with ``private_key_jwt`` and hit IB's cloud
   endpoints from anywhere — no local process at all.

Two auth flavors, chosen at construction:

* :class:`CPGatewayAuth` — retail. Hits ``https://localhost:5000/v1/api/*``; the running CP
  Gateway holds the session cookie. Requires ``clientportal.gw`` up and logged-in via browser.
* :class:`OAuth2JWTAuth` — institutional. Signs a private-key JWT, exchanges for a bearer token,
  hits ``https://api.ibkr.com/v1/api/*`` directly. Token refresh handled here.

**Live-order gate.** Same contract as ``IBBroker`` and ``KrakenBroker``: ``enable_live_orders=
True`` must be set explicitly at construction. Default is paper-only.

**Not yet built** (documented for the follow-up):

* WebSocket streaming (``/ws``) — this adapter is REST-only for the first pass. The Broker
  protocol doesn't need streaming; when it lands, it plugs into the same conid-based contract
  cache.
* L2 depth-of-book — the Web API's book endpoints are more limited than TWS's ``reqMktDepth``;
  use ``IBBroker.request_l2_book`` when full depth is needed.
* Algo orders (VWAP/TWAP/etc) — Web API supports the basic types; some advanced ones are
  TWS-only. Route those via ``IBBroker.place_algo_order``.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from ..logging_setup import get_logger
from .base import Broker, BrokerError, OrderRejected
from .models import Account, Candle, Order, OrderAction, Position, Quote

log = get_logger(__name__)


# Field IDs the Web API returns from /iserver/marketdata/snapshot. IBKR uses opaque numeric ids
# rather than named JSON keys; this mapping is the canonical translation for the fields we
# actually read. Reference: iserver marketdata field reference.
_MD_FIELD_LAST = "31"
_MD_FIELD_BID = "84"
_MD_FIELD_ASK = "86"
_MD_FIELD_BID_SIZE = "88"
_MD_FIELD_ASK_SIZE = "85"
_MD_FIELD_VOLUME = "7311"
_MD_FIELD_OPEN = "7295"
_MD_FIELD_HIGH = "7296"
_MD_FIELD_LOW = "7297"
_MD_FIELDS_ALL = ",".join([
    _MD_FIELD_LAST, _MD_FIELD_BID, _MD_FIELD_ASK, _MD_FIELD_BID_SIZE, _MD_FIELD_ASK_SIZE,
    _MD_FIELD_VOLUME, _MD_FIELD_OPEN, _MD_FIELD_HIGH, _MD_FIELD_LOW,
])

# Bar period + size translation for /iserver/marketdata/history. The Web API takes both a
# ``period`` (how far back) and a ``bar`` (bar granularity) with its own syntax.
_INTERVAL_TO_BAR: dict[str, str] = {
    "OneMinute": "1min", "FiveMinutes": "5min", "FifteenMinutes": "15min",
    "ThirtyMinutes": "30min", "OneHour": "1h", "FourHours": "4h",
    "OneDay": "1d", "OneWeek": "1w",
}


# ---- auth ----------------------------------------------------------------------------------


class IBWebAuth(ABC):
    """Base class for the two auth flavors. Wraps an ``httpx.Client`` with the right base URL
    plus per-request auth (session cookie or bearer token)."""

    @property
    @abstractmethod
    def base_url(self) -> str:
        """Fully-qualified prefix, e.g. ``https://localhost:5000/v1/api``."""

    @abstractmethod
    def apply(self, request: httpx.Request) -> None:
        """Mutate ``request`` in place with the auth surface for this flavor."""

    def new_client(self, timeout: float = 30.0, verify: bool = True) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=timeout, verify=verify,
                             headers={"User-Agent": "FRM-Claude/1.0",
                                      "Accept": "application/json"})


@dataclass
class CPGatewayAuth(IBWebAuth):
    """Client Portal Gateway auth (retail). The Gateway holds the session cookie server-side;
    all we do is talk to its localhost endpoint. ``verify_ssl=False`` because the Gateway ships
    a self-signed cert.
    """
    host: str = "localhost"
    port: int = 5000
    verify_ssl: bool = False

    @property
    def base_url(self) -> str:
        return f"https://{self.host}:{self.port}/v1/api"

    def apply(self, request: httpx.Request) -> None:
        # Session cookie is set by the Gateway on the connection; nothing to add per-request.
        return

    def new_client(self, timeout: float = 30.0, verify: bool | None = None) -> httpx.Client:
        return super().new_client(timeout=timeout,
                                    verify=self.verify_ssl if verify is None else verify)


@dataclass
class OAuth2JWTAuth(IBWebAuth):
    """OAuth 2.0 ``private_key_jwt`` (institutional). Signs a client-assertion JWT with a
    private key, exchanges at the token endpoint for a bearer token, caches until the token's
    ``expires_in`` window is nearly up (60s early refresh).

    The JWT signing itself needs ``PyJWT`` + ``cryptography`` for RS256/ES256 — the repo already
    has ``cryptography``. ``PyJWT`` is imported lazily so this class stays importable without it.
    """
    client_id: str = ""
    private_key_pem: str = ""                   # full PEM string of the client's private key
    key_id: str = ""                            # ``kid`` header — matches the JWK registered with IB
    token_url: str = "https://api.ibkr.com/oauth2/api/v1/token"
    api_base: str = "https://api.ibkr.com/v1/api"
    scope: str = "trading"

    _token: str = field(default="", init=False, repr=False)
    _expires_at: float = field(default=0.0, init=False, repr=False)

    @property
    def base_url(self) -> str:
        return self.api_base

    def _mint_client_assertion(self) -> str:
        """Sign a short-lived JWT proving the client's identity to IB's token endpoint."""
        try:
            import jwt                          # noqa: PLC0415 — lazy import
        except ImportError as e:
            raise BrokerError(
                "IBWebBroker OAuth2JWTAuth requires 'PyJWT' (`uv add PyJWT` or "
                "`pip install PyJWT`) for signing the client_assertion."
            ) from e
        now = int(time.time())
        payload = {
            "iss": self.client_id,
            "sub": self.client_id,
            "aud": self.token_url,
            "iat": now,
            "exp": now + 300,          # 5 min — IB rejects long-lived assertions
            "jti": f"{self.client_id}-{now}",
        }
        headers = {"alg": "RS256", "typ": "JWT"}
        if self.key_id:
            headers["kid"] = self.key_id
        return jwt.encode(payload, self.private_key_pem, algorithm="RS256", headers=headers)

    def _refresh(self, client: httpx.Client | None = None) -> None:
        """Exchange the JWT for a bearer token. Called lazily on ``apply``."""
        owns = client is None
        client = client or httpx.Client(timeout=30.0)
        try:
            r = client.post(self.token_url, data={
                "grant_type": "client_credentials",
                "client_assertion_type": (
                    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"),
                "client_assertion": self._mint_client_assertion(),
                "scope": self.scope,
            })
        finally:
            if owns:
                client.close()
        if r.status_code >= 400:
            raise BrokerError(f"IB OAuth2 token exchange failed ({r.status_code}): {r.text[:200]}")
        body = r.json()
        self._token = body.get("access_token", "")
        # 60s early refresh window so a request never fires with a token about to expire.
        self._expires_at = time.time() + int(body.get("expires_in", 600)) - 60

    def apply(self, request: httpx.Request) -> None:
        if not self._token or time.time() >= self._expires_at:
            self._refresh()
        request.headers["Authorization"] = f"Bearer {self._token}"


# ---- broker adapter ------------------------------------------------------------------------


class IBWebBroker(Broker):
    """Interactive Brokers Web API adapter. Speaks REST/JSON via the auth surface passed in."""

    name = "interactive-brokers-web"
    venue = "ib_web"

    def __init__(
        self,
        auth: IBWebAuth,
        *,
        client: httpx.Client | None = None,
        enable_live_orders: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self._auth = auth
        self._client = client or auth.new_client(timeout=timeout)
        self._owns_client = client is None
        self._enable_live_orders = enable_live_orders
        # conid cache keyed by (symbol, sec_type). Populated on first quote/order for a symbol
        # since the Web API is conid-driven — no bare-symbol endpoint accepts a ticker directly.
        self._conid_cache: dict[tuple[str, str], int] = {}
        # Per-symbol sec_type override; empty means "resolve as STK". Callers register futures /
        # options / bonds here so the Broker protocol's plain-symbol surface still gets the right
        # contract type. See :meth:`set_sec_type` and :meth:`resolve_conid`.
        self._sec_type_overrides: dict[str, str] = {}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "IBWebBroker":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---- low-level HTTP -----------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        req = self._client.build_request(method, path, **kwargs)
        self._auth.apply(req)
        r = self._client.send(req)
        if r.status_code >= 400:
            raise BrokerError(f"IB Web API {method} {path} -> {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError:
            return {}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, json=body or {})

    def _delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    # ---- session keep-alive -------------------------------------------------

    def tickle(self) -> None:
        """Ping the session so the CP Gateway doesn't time out. No-op on OAuth2 (token-based).

        The CP Gateway drops the session after a few minutes of inactivity; a long-running
        monitor should call this on a cadence (or on every poll).
        """
        if not isinstance(self._auth, CPGatewayAuth):
            return
        try:
            self._post("/tickle")
        except BrokerError as e:
            log.warning("ibweb.tickle.failed", error=str(e))

    # ---- contract resolution -----------------------------------------------

    def set_sec_type(self, symbol: str, sec_type: str) -> None:
        """Override the default STK resolution for a symbol.

        The Broker protocol surface (``quote``, ``candles``, ``place_order``) takes plain symbol
        strings and has no sec_type parameter. When a monitor needs a non-STK contract — an ES
        futures front-month rather than the Eversource stock ticker — the caller registers the
        override once at setup time. Subsequent calls transparently resolve against the right
        contract type.
        """
        self._sec_type_overrides[symbol.upper()] = sec_type.upper()
        # Invalidate any prior STK cache entry so the next call resolves against the new type.
        for key in list(self._conid_cache.keys()):
            if key[0] == symbol.upper() and key[1] != sec_type.upper():
                del self._conid_cache[key]

    def _sec_type_for(self, symbol: str) -> str:
        return self._sec_type_overrides.get(symbol.upper(), "STK")

    def resolve_conid(self, symbol: str, sec_type: str | None = None) -> int:
        """Symbol → IB contract id (``conid``). Cached per (symbol, sec_type).

        Sec_type resolution order: explicit ``sec_type`` argument (call-site override) → registered
        symbol override via :meth:`set_sec_type` → default ``STK``. Futures (``sec_type='FUT'``)
        route through the dedicated ``/trsrv/futures`` endpoint so the front-month conid is picked
        deterministically; every other type goes through the general ``/iserver/secdef/search``.
        """
        stype = (sec_type or self._sec_type_for(symbol)).upper()
        key = (symbol.upper(), stype)
        if key in self._conid_cache:
            return self._conid_cache[key]

        if stype == "FUT":
            conid = self._resolve_futures_front_month(symbol)
        else:
            body = self._post("/iserver/secdef/search", {"symbol": symbol, "name": False,
                                                           "secType": stype})
            if not isinstance(body, list) or not body:
                raise BrokerError(f"IB Web API: no contracts found for {symbol!r} ({stype})")
            cand = body[0]
            conid = int(cand.get("conid") or 0)
            if conid <= 0:
                raise BrokerError(f"IB Web API: contract for {symbol!r} has no conid")

        self._conid_cache[key] = conid
        return conid

    def _resolve_futures_front_month(self, symbol: str) -> int:
        """Pick the front-month futures contract for a root symbol via ``/trsrv/futures``.

        ``/iserver/secdef/search`` returns futures roots (one entry per root) but not the
        individual expiration conids that trading needs — the real conid lives one level down at
        ``/trsrv/futures?symbols=ES``, which returns an array per root of every listed expiration.
        Front-month = earliest ``expirationDate`` strictly after today. Falls back to the earliest
        entry if none are strictly in the future (a stale mid-roll response), and raises with an
        actionable message if the root is unknown to IB.
        """
        body = self._get("/trsrv/futures", {"symbols": symbol})
        # Response shape: {"ES": [{"conid": ..., "expirationDate": "20260918", ...}, ...]}
        entries = body.get(symbol.upper()) if isinstance(body, dict) else None
        if not entries:
            raise BrokerError(f"IB Web API: no futures listed for root {symbol!r}. "
                              f"Check the root symbol matches IB's convention (ES, NQ, CL, GC, ZN).")
        today_yyyymmdd = int(datetime.now(UTC).strftime("%Y%m%d"))
        def _exp(e: dict) -> int:
            try:
                return int(str(e.get("expirationDate") or "99999999"))
            except (TypeError, ValueError):
                return 99999999
        future_entries = [e for e in entries if _exp(e) > today_yyyymmdd]
        pick = min(future_entries, key=_exp) if future_entries else min(entries, key=_exp)
        conid = int(pick.get("conid") or 0)
        if conid <= 0:
            raise BrokerError(f"IB Web API: futures front-month for {symbol!r} has no conid")
        return conid

    # ---- Broker protocol ----------------------------------------------------

    def accounts(self) -> list[Account]:
        body = self._get("/iserver/accounts")
        # Body shape: {"accounts": ["DUXXXX", ...], "selectedAccount": "DUXXXX", ...}
        acc_ids = body.get("accounts") or []
        if not acc_ids:
            return []
        selected = body.get("selectedAccount") or acc_ids[0]
        return [Account(type="IB", number=acc, status="Active",
                         isPrimary=(acc == selected)) for acc in acc_ids]

    def positions(self, account_number: str) -> list[Position]:
        # Page 0 is the first ~30 positions; a real portfolio wants to page through, but for
        # our paper account sizes 0 is fine. Callers can extend by paging when needed.
        body = self._get(f"/portfolio/{account_number}/positions/0")
        out: list[Position] = []
        for row in body if isinstance(body, list) else []:
            qty = float(row.get("position") or 0.0)
            if qty == 0:
                continue
            out.append(Position(
                symbol=row.get("contractDesc") or row.get("ticker") or "",
                symbolId=int(row.get("conid") or 0),
                openQuantity=qty,
                averageEntryPrice=float(row.get("avgCost") or 0.0),
                currentPrice=float(row.get("mktPrice") or 0.0),
                totalCost=float(row.get("avgCost") or 0.0) * qty,
            ))
        return out

    def quote(self, symbol: str) -> Quote:
        return self.quotes([symbol])[0]

    def quotes(self, symbols: list[str]) -> list[Quote]:
        conids = [self.resolve_conid(s) for s in symbols]
        body = self._get("/iserver/marketdata/snapshot",
                          {"conids": ",".join(str(c) for c in conids),
                           "fields": _MD_FIELDS_ALL})
        # snapshot returns a list of dicts, one per conid, in the SAME order requested.
        rows = body if isinstance(body, list) else []
        by_conid = {int(r.get("conid") or 0): r for r in rows}
        out: list[Quote] = []
        for sym, conid in zip(symbols, conids, strict=True):
            r = by_conid.get(conid, {})
            out.append(Quote(
                symbol=sym, symbolId=conid,
                bidPrice=_num(r.get(_MD_FIELD_BID)),
                askPrice=_num(r.get(_MD_FIELD_ASK)),
                lastTradePrice=_num(r.get(_MD_FIELD_LAST)),
            ))
        return out

    def candles(self, symbol: str, start: datetime, end: datetime,
                interval: str = "OneDay") -> list[Candle]:
        conid = self.resolve_conid(symbol)
        # Web API takes a period string ("1y", "6m", "30d") rather than start/end. Derive from
        # the requested window.
        days = max(1, (end - start).days)
        period = f"{days}d" if days <= 30 else f"{max(1, days // 30)}m"
        bar = _INTERVAL_TO_BAR.get(interval, "1d")
        try:
            body = self._get("/iserver/marketdata/history",
                              {"conid": str(conid), "period": period, "bar": bar})
        except BrokerError as e:
            # IB Web's history endpoint 500s with "Chart data unavailable" on symbols whose
            # snapshot hasn't fully warmed up yet, or on instruments with no historical chart
            # entitlement on this account. Return empty so LiveMonitor skips this symbol
            # (insufficient_history warning) rather than killing the whole poll loop.
            msg = str(e).lower()
            if "chart data unavailable" in msg or "-> 500" in msg or "-> 404" in msg:
                return []
            raise
        rows = body.get("data") or []
        out: list[Candle] = []
        for row in rows:
            ts_ms = int(row.get("t") or 0)
            when = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
            out.append(Candle(
                start=when, end=when,
                open=float(row.get("o") or 0.0), high=float(row.get("h") or 0.0),
                low=float(row.get("l") or 0.0), close=float(row.get("c") or 0.0),
                volume=int(float(row.get("v") or 0.0)),
            ))
        return out

    def equity(self, account_number: str, currency: str = "USD") -> float:
        body = self._get(f"/portfolio/{account_number}/summary")
        # Summary keys include netliquidation.{amount,currency}; sum matching currency.
        row = body.get("netliquidation") if isinstance(body, dict) else None
        if isinstance(row, dict) and str(row.get("currency", "")).upper() == currency.upper():
            try:
                return float(row.get("amount") or 0.0)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    # ---- orders -------------------------------------------------------------

    def place_order(self, order: Order) -> Order:
        if not self._enable_live_orders:
            raise OrderRejected(
                "IBWebBroker: live orders disabled. Wrap this broker in PaperBroker(feed=...) "
                "for simulated fills, or construct with enable_live_orders=True after the human "
                "go-live confirmation."
            )
        if not order.accountId:
            raise OrderRejected("IBWebBroker: place_order requires order.accountId")
        conid = self.resolve_conid(order.symbol)
        body = {"orders": [{
            "conid": conid,
            "orderType": order.orderType.value.upper(),
            "side": "BUY" if order.action == OrderAction.BUY else "SELL",
            "tif": order.timeInForce.value.upper(),
            "quantity": order.totalQuantity,
            **({"price": order.limitPrice} if order.limitPrice is not None else {}),
        }]}
        resp = self._post(f"/iserver/account/{order.accountId}/orders", body)
        # Web API sometimes returns a confirmation prompt (list of {id, message}) that must be
        # answered before the order fires. Auto-confirm any that come back — a paper trader
        # doesn't want to hand-approve every one, and the confirmation is a soft warning.
        while isinstance(resp, list) and resp and "id" in resp[0] and "message" in resp[0]:
            reply_id = resp[0]["id"]
            resp = self._post(f"/iserver/reply/{reply_id}", {"confirmed": True})
        if isinstance(resp, list) and resp and "order_id" in resp[0]:
            order.id = int(resp[0]["order_id"])
        log.info("ibweb.order.submitted", symbol=order.symbol, qty=order.totalQuantity,
                 action=order.action.value, order_id=order.id)
        return order

    def cancel_order(self, account_number: str, order_id: int) -> None:
        if not self._enable_live_orders:
            raise BrokerError("IBWebBroker: live orders disabled; nothing to cancel.")
        self._delete(f"/iserver/account/{account_number}/order/{order_id}")


# ---- helpers -------------------------------------------------------------------------------


def _num(v: Any) -> float | None:
    """Parse IB's price fields, which come as strings and may include prefix chars like 'C'
    (closing price marker). Returns None for empty / unparseable."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).lstrip("Cc").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None
