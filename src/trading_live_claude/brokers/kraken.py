"""KrakenBroker — implements the ``Broker`` protocol against Kraken's public + private REST APIs.

Purpose: give the ``Router`` an execution surface for the crypto sleeve, so orders that clear the
router's risk gates can actually fill (via ``PaperBroker`` first, and eventually — behind the
go-live gate — via Kraken's live private API).

Design constraints, in tension with each other:

* **Interface parity.** Every existing consumer (the router, the paper broker, the live monitor)
  addresses a broker through the same ``Broker`` protocol — same method names, same models. This
  file translates Kraken's shape into that surface without leaking any Kraken-specific type outward.
* **Fractional sizing.** Crypto is fractional; ``Order.totalQuantity`` is already ``float``, so the
  model can carry a fractional quantity end-to-end. The router will still round on the way in for
  equities, and pass through for crypto — the broker itself never quantizes.
* **Live placement is gated.** ``place_order`` REFUSES to send unless ``enable_live_orders=True``
  is passed at construction. This is a per-instance switch that no code path in the repo flips on
  its own; the go-live decision is a human one. The default is dry: orders raise a clear error
  rather than silently signing anything.
* **Testable without the network.** The HTTP surface is a single ``httpx.Client`` slot that a test
  can inject with a ``MockTransport``. The ``private_post`` helper is the existing signing core
  from ``kraken_auth.py`` — reused, not duplicated.

Pair conventions. The routed symbol in ``CRYPTO_SLEEVE`` is ``BTC/USD``; Kraken's Ticker/OHLC use
``XBTUSD`` on the wire and ``XXBTZUSD`` in the response key. The small ``_KRAKEN_PAIRS`` table
covers the sleeve today; anything outside it round-trips as the raw code the caller passes.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx

from ..logging_setup import get_logger
from .base import Broker, BrokerError, OrderRejected
from .kraken_auth import KRAKEN_REST, KrakenAuthError, private_post
from .models import (
    Account,
    Candle,
    Order,
    OrderAction,
    OrderType,
    Position,
    Quote,
)

log = get_logger(__name__)

# Routed symbol (as used everywhere else in this project) → Kraken wire pair.
# Response keys can differ ("XXBTZUSD" instead of "XBTUSD") — that mapping is done at read time
# from the payload keys themselves, so we do not have to enumerate it here.
_KRAKEN_PAIRS: dict[str, str] = {
    "BTC/USD": "XBTUSD",
    "ETH/USD": "ETHUSD",
    "XMR/USD": "XMRUSD",
    "XRP/USD": "XRPUSD",
    "XLM/USD": "XLMUSD",
    "LINK/USD": "LINKUSD",
    "PAXG/USD": "PAXGUSD",
}


def to_kraken_pair(symbol: str) -> str:
    """Routed symbol (``BTC/USD``) → Kraken wire pair (``XBTUSD``). Pass through for unknowns."""
    return _KRAKEN_PAIRS.get(symbol, symbol)


class KrakenBroker(Broker):
    """Broker adapter for Kraken. Public data works without creds; private data requires them.

    ``place_order`` requires ``enable_live_orders=True`` — the switch is explicit and NEVER flipped
    from application code. This is the go-live gate.
    """

    name = "kraken"
    venue = "kraken"

    def __init__(
        self,
        *,
        api_key: str = "",
        api_secret: str = "",
        client: httpx.Client | None = None,
        enable_live_orders: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._client = client or httpx.Client(
            timeout=timeout, headers={"User-Agent": "FRM-Claude/1.0"}
        )
        self._owns_client = client is None
        self._enable_live_orders = enable_live_orders

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "KrakenBroker":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---- read-only surface ---------------------------------------------------

    def accounts(self) -> list[Account]:
        """Kraken has one account per user; synthesized so the router sees a stable id."""
        return [Account(type="Cash", number="KRAKEN", status="Active", isPrimary=True)]

    def positions(self, account_number: str) -> list[Position]:
        """Non-zero spot balances, translated into ``Position`` entries.

        Kraken's ``Balance`` endpoint returns every asset held on the account keyed by its Kraken
        asset code (``XXBT``, ``XETH``, ``ZUSD``). USD/fiat balances are cash and are surfaced
        through :meth:`equity` rather than as positions. Without credentials this returns [].
        """
        if not (self._api_key and self._api_secret):
            return []
        try:
            balances = private_post(
                "/0/private/Balance",
                key=self._api_key, secret=self._api_secret, client=self._client,
            )
        except KrakenAuthError as e:
            raise BrokerError(f"Kraken Balance failed: {e}") from e

        out: list[Position] = []
        for asset, amount_str in balances.items():
            amount = float(amount_str)
            if amount == 0 or _is_fiat_asset(asset):
                continue
            symbol = _asset_to_routed_symbol(asset)
            out.append(Position(
                symbol=symbol, symbolId=0,
                openQuantity=amount,
                # Kraken Balance does not include a mark-to-market; leave price fields at 0 and let
                # the caller pull a fresh quote if it needs current value.
                currentPrice=0.0, averageEntryPrice=0.0, totalCost=0.0,
            ))
        return out

    def quote(self, symbol: str) -> Quote:
        return self.quotes([symbol])[0]

    def quotes(self, symbols: list[str]) -> list[Quote]:
        """Public Ticker for one or more pairs. One HTTP round-trip regardless of length."""
        if not symbols:
            return []
        wire = ",".join(to_kraken_pair(s) for s in symbols)
        payload = self._get_public("/0/public/Ticker", {"pair": wire})
        result = payload.get("result", {})
        # Kraken responds keyed by its canonical pair code (e.g. XXBTZUSD), which is not the wire
        # code we sent. Build a case-insensitive substring lookup: for each requested symbol, find
        # the response key that contains the wire pair or its trailing form.
        out: list[Quote] = []
        for sym in symbols:
            data = _find_ticker(result, to_kraken_pair(sym))
            if data is None:
                # No matching key; return an empty quote rather than raising, so the paper broker's
                # "no reference price" reject path is what handles the missing datum.
                out.append(Quote(symbol=sym, symbolId=0))
                continue
            bid = _first_price(data.get("b"))
            ask = _first_price(data.get("a"))
            last = _first_price(data.get("c"))
            out.append(Quote(
                symbol=sym, symbolId=0,
                bidPrice=bid, askPrice=ask, lastTradePrice=last,
            ))
        return out

    def candles(self, symbol: str, start: datetime, end: datetime,
                interval: str = "OneDay") -> list[Candle]:
        """Public OHLC bars for ``symbol`` between ``start`` and ``end`` (inclusive).

        ``interval`` accepts the project's canonical strings (``OneDay``, ``OneHour``, ``OneMinute``)
        and translates to Kraken minutes. Kraken caps at ~720 bars per request — the caller must
        page (see :mod:`...data.kraken_ohlc` for the daily flow); this method returns whatever the
        first request gives back, filtered to the window.
        """
        payload = self._get_public("/0/public/OHLC",
                                   {"pair": to_kraken_pair(symbol),
                                    "interval": _interval_minutes(interval)})
        result = payload.get("result", {})
        series = next((v for k, v in result.items() if k != "last"), None)
        if not series:
            return []
        out: list[Candle] = []
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        for row in series:
            ts = int(row[0])
            if ts < start_ts or ts > end_ts:
                continue
            when = datetime.fromtimestamp(ts, tz=UTC)
            out.append(Candle(
                start=when, end=when,
                open=float(row[1]), high=float(row[2]),
                low=float(row[3]), close=float(row[4]),
                volume=int(float(row[6])),
            ))
        return out

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        """Total cash in ``currency`` (USD by convention on Kraken — CAD default is protocol parity).

        Sums Kraken's fiat balances. The router's exposure math cares about ``equity`` monotonically
        — a rough number here is safe; a wrong-currency number is not — so we intentionally return
        only the requested-currency fiat rather than trying to mark crypto positions in real time.
        """
        if not (self._api_key and self._api_secret):
            return 0.0
        try:
            balances = private_post(
                "/0/private/Balance",
                key=self._api_key, secret=self._api_secret, client=self._client,
            )
        except KrakenAuthError:
            return 0.0
        target = f"Z{currency.upper()}"           # Kraken fiat prefix (ZUSD, ZCAD, ZEUR, ...)
        return float(balances.get(target, 0.0))

    # ---- order surface -------------------------------------------------------

    def place_order(self, order: Order) -> Order:
        """Submit an order to Kraken. REFUSES unless ``enable_live_orders=True`` at construction.

        The refusal is loud (an ``OrderRejected`` with an explicit reason) rather than a silent
        no-op, so the router's fill journal shows exactly why nothing hit the market. When enabled,
        maps the project's ``Order`` fields onto Kraken's ``AddOrder`` schema — ``symbol`` translated
        to the wire pair, ``totalQuantity`` passed as ``volume`` (fractional respected).
        """
        if not self._enable_live_orders:
            raise OrderRejected(
                "KrakenBroker: live orders disabled. Wrap this broker in PaperBroker(feed=...) "
                "for simulated fills, or construct with enable_live_orders=True after the human "
                "go-live confirmation."
            )
        if not (self._api_key and self._api_secret):
            raise OrderRejected("KrakenBroker: no API credentials configured.")

        body: dict[str, Any] = {
            "pair": to_kraken_pair(order.symbol),
            "type": "buy" if order.action == OrderAction.BUY else "sell",
            "ordertype": _order_type_wire(order.orderType),
            "volume": str(order.totalQuantity),
        }
        if order.orderType in (OrderType.LIMIT, OrderType.STOP_LIMIT) and order.limitPrice is not None:
            body["price"] = str(order.limitPrice)
        if order.orderType in (OrderType.STOP, OrderType.STOP_LIMIT) and order.stopPrice is not None:
            body["price2"] = str(order.stopPrice)

        try:
            result = private_post(
                "/0/private/AddOrder", key=self._api_key, secret=self._api_secret,
                data=body, client=self._client,
            )
        except KrakenAuthError as e:
            raise OrderRejected(f"Kraken AddOrder rejected: {e}") from e

        txids = result.get("txid") or []
        if txids:
            # Kraken returns transaction ids as strings; stash the first for reference.
            order.id = _txid_to_int(txids[0])
        log.info(
            "kraken.order.submitted",
            symbol=order.symbol,
            action=order.action.value,
            qty=order.totalQuantity,
            txid=txids[0] if txids else None,
        )
        return order

    def cancel_order(self, account_number: str, order_id: int) -> None:
        if not self._enable_live_orders:
            raise BrokerError("KrakenBroker: live orders disabled; nothing to cancel.")
        if not (self._api_key and self._api_secret):
            raise BrokerError("KrakenBroker: no API credentials configured.")
        try:
            private_post(
                "/0/private/CancelOrder",
                key=self._api_key, secret=self._api_secret,
                data={"txid": str(order_id)}, client=self._client,
            )
        except KrakenAuthError as e:
            raise BrokerError(f"Kraken CancelOrder failed: {e}") from e

    # ---- helpers -------------------------------------------------------------

    def _get_public(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        # Small retry so a transient 5xx doesn't take down a monitor loop.
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                r = self._client.get(KRAKEN_REST + path, params=params)
                data = r.json()
                if data.get("error"):
                    raise BrokerError(f"Kraken {path} error: {data['error']}")
                return data
            except (httpx.HTTPError, BrokerError) as e:
                last_err = e
                if attempt == 0:
                    time.sleep(0.5)
                    continue
                raise
        assert last_err is not None       # unreachable, satisfies mypy
        raise last_err


# ---- module-level helpers ----------------------------------------------------


_FIAT_ASSETS = frozenset(("ZUSD", "ZCAD", "ZEUR", "ZGBP", "ZJPY", "USDC", "USDT", "DAI"))


def _is_fiat_asset(asset: str) -> bool:
    return asset.upper() in _FIAT_ASSETS


# Kraken canonical asset code → the routed symbol most naturally paired against USD.
_KRAKEN_ASSET_TO_ROUTED: dict[str, str] = {
    "XXBT": "BTC/USD", "XBT": "BTC/USD",
    "XETH": "ETH/USD", "ETH": "ETH/USD",
    "XXMR": "XMR/USD", "XMR": "XMR/USD",
    "XXRP": "XRP/USD", "XRP": "XRP/USD",
    "XXLM": "XLM/USD", "XLM": "XLM/USD",
    "LINK": "LINK/USD",
    "PAXG": "PAXG/USD",
}


def _asset_to_routed_symbol(asset: str) -> str:
    """Kraken asset code (``XXBT``) → routed symbol (``BTC/USD``). Pass through unknown assets."""
    return _KRAKEN_ASSET_TO_ROUTED.get(asset, asset)


def _find_ticker(result: dict[str, Any], wire_pair: str) -> dict[str, Any] | None:
    """Kraken responds under its canonical key (e.g. ``XXBTZUSD``), not the wire code we sent.

    Match by suffix so ``XBTUSD`` (wire) finds ``XXBTZUSD`` (response) via the shared trailing form.
    """
    if wire_pair in result:
        return result[wire_pair]
    # Compare the trailing letters — the response key strips wrapper letters but keeps the pair.
    for key, val in result.items():
        if key.endswith(wire_pair) or wire_pair.endswith(key.replace("X", "").replace("Z", "")):
            return val
    return None


def _first_price(field: list[str] | None) -> float | None:
    if not field:
        return None
    try:
        return float(field[0])
    except (TypeError, ValueError):
        return None


_INTERVAL_MINUTES: dict[str, int] = {
    "OneMinute": 1, "FiveMinutes": 5, "FifteenMinutes": 15,
    "ThirtyMinutes": 30, "OneHour": 60, "FourHours": 240,
    "OneDay": 1440,
}


def _interval_minutes(interval: str) -> int:
    return _INTERVAL_MINUTES.get(interval, 1440)


def _order_type_wire(t: OrderType) -> str:
    return {
        OrderType.MARKET: "market",
        OrderType.LIMIT: "limit",
        OrderType.STOP: "stop-loss",
        OrderType.STOP_LIMIT: "stop-loss-limit",
    }.get(t, "market")


def _txid_to_int(txid: str) -> int:
    """Kraken txids are strings (``OZAA6H-FQI3S-DK6GHJ``). We hash-fold to an int so the router,
    whose fill journal stores integer order ids, has a stable numeric handle. Not reversible; the
    original txid stays in the log line.
    """
    return abs(hash(txid)) % (2**31 - 1)
