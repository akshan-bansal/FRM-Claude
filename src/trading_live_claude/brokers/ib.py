"""IBBroker — Interactive Brokers adapter for the ``Broker`` protocol.

The purpose IB serves in this project is DIFFERENT from Questrade or Kraken. QT is our default
equity broker (Cdn-listed and US equities, ETFs), Kraken is the crypto venue. IB opens up asset
classes and venues neither can reach:

* **Bonds** (corporates, treasuries, munis) — QT retail has no continuous bond order book.
* **Commodities futures** (ES, NQ, CL, GC, ZC, ZS, ZW …) — real front-month contracts, not the
  ETF proxies the current framework leans on (USO, GLD, etc).
* **Global equities and ETFs** — LSE, ASX, HKEX, TSE plus the North-American venues we already
  cover, all through one adapter.
* **Options** — equity and index options with the full Greeks surface.
* **Level II depth-of-book** — IB's ``reqMktDepth`` streams the top-N bids/asks per exchange,
  which the QT/Kraken retail feeds do not expose.
* **HFT-adjacent order types** — VWAP, TWAP, PEGGED, iceberg, discretionary — routes IB supports
  natively; our current router only speaks Market and Limit.

Two runtime paths, chosen at construction:

1. **``ib_insync``** — a widely-used Python wrapper over IB's TWS/Gateway socket API. Requires
   a running TWS or IB Gateway process (paper or live) with the API enabled. Standard install
   route for local development. If ``ib_insync`` isn't on the interpreter path, construction
   raises with an actionable install hint rather than a silent failure.
2. **MCP** — the ``interactive-brokers-mcp`` server declared in ``.mcp.json``. Different
   surface (JSON-RPC tools loaded into Claude's tool namespace); used for exploratory
   interactive work, not the runtime trading path. This module doesn't call the MCP — that
   runs above the code, at the assistant tool layer.

**Live-order gate.** Same contract as ``KrakenBroker``: ``enable_live_orders=True`` MUST be
set at construction for ``place_order`` / ``cancel_order`` to send anything. Default is
paper-only. The gate is per-instance and no code path in the repo flips it on its own.

**Paper-first.** The runtime is wrapped in ``PaperBroker(feed=IBBroker(...))`` by
``scripts/paper_ib.py`` — orders are simulated locally against IB's live quotes, and IB's
private order path is untouched even when ``enable_live_orders=True`` (the paper broker
short-circuits before dispatch).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from ..logging_setup import get_logger
from .base import Broker, BrokerError, OrderRejected
from .models import Account, Candle, Order, OrderAction, Position, Quote

log = get_logger(__name__)


# ---- asset-class expansion ---------------------------------------------------
# IB lets us reach classes QT/Kraken cannot; enumerate them explicitly so downstream code
# (router class scaling, sweep protocols, allocator sleeves) can gate on the value rather
# than sniffing symbol shapes.

IBAssetClass = Literal[
    "stock",       # STK — equities and ETFs (Nasdaq, NYSE, TSX, LSE, etc.)
    "future",      # FUT — commodity + index + rate futures (ES, NQ, CL, GC, ZC, ZN, …)
    "option",      # OPT — equity + index options (calls/puts, with strike + expiry)
    "bond",        # BOND — corporate + treasury + municipal bonds
    "forex",       # CASH — spot FX pairs (EUR.USD, USD.JPY, …)
    "index",       # IND — cash index (SPX, NDX) for quote reference
    "fund",        # FUND — mutual funds (limited liquidity)
    "crypto",      # CRYPTO — IB's paper crypto surface (limited pairs vs Kraken)
]

# Time-in-force values IB supports beyond QT's DAY/GTC. Adding here so callers get compile-time
# feedback (via Literal) rather than a silent broker-rejected order.
IBTimeInForce = Literal["DAY", "GTC", "IOC", "FOK", "GTD", "OPG", "AUC"]

# High-frequency / algo order types beyond MKT/LMT. Routed as IB's ``orderType`` string.
IBAlgoOrderType = Literal[
    "MKT", "LMT", "STP", "STP LMT", "TRAIL", "TRAIL LIMIT",
    "MOC",        # Market on Close
    "LOC",        # Limit on Close
    "VWAP",       # Volume-Weighted Average Price algo
    "TWAP",       # Time-Weighted Average Price algo
    "MIDPRICE",   # Attempts to fill at NBBO midpoint
    "PEG MID",    # Pegged to midpoint with offset
    "PEG BEST",   # Pegged to NBBO
    "ICEBERG",    # Displayed size < total; hidden remainder
]


@dataclass(frozen=True)
class IBContract:
    """Structured contract spec for a symbol on IB — richer than a bare ticker string.

    IB requires ``secType`` + ``exchange`` + ``currency`` at minimum for anything past a plain
    US equity. This dataclass carries the extra fields explicitly so a caller resolving
    ``AAPL`` (SMART/USD stock) vs ``ES`` (GLOBEX front-month future) vs ``EUR.USD`` (IDEALPRO
    forex) knows which they're asking for.
    """
    symbol: str
    sec_type: IBAssetClass = "stock"
    exchange: str = "SMART"           # SMART routing is IB's cross-venue best-execution
    currency: str = "USD"
    # Futures / options only. Expiry format YYYYMMDD (front month typically resolved by
    # ib_insync's Contract search when omitted). Multiplier auto-derives for standard contracts.
    expiry: str = ""
    strike: float | None = None       # options only
    right: str | None = None          # options only: "C" or "P"
    multiplier: str = ""              # futures / options; empty = IB default
    trading_class: str = ""
    # Primary exchange disambiguates SMART-routed dual-listings (AAPL NASDAQ vs AAPL ARCA).
    primary_exchange: str = ""


# ---- L2 book snapshot -------------------------------------------------------
# Not a Broker-protocol requirement — IB-specific. Emitted by ``request_l2_book`` for callers
# that want the depth-of-book surface (research on microstructure, execution-cost estimates).

@dataclass(frozen=True)
class L2Level:
    price: float
    size: float
    exchange: str = ""
    market_maker: str = ""


@dataclass(frozen=True)
class L2Book:
    symbol: str
    as_of: datetime
    bids: tuple[L2Level, ...] = field(default_factory=tuple)   # sorted best-first
    asks: tuple[L2Level, ...] = field(default_factory=tuple)   # sorted best-first

    @property
    def spread_bps(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        best_bid = self.bids[0].price
        best_ask = self.asks[0].price
        mid = (best_bid + best_ask) / 2.0
        return None if mid <= 0 else (best_ask - best_bid) / mid * 10_000.0


# ---- adapter ----------------------------------------------------------------


class IBBroker(Broker):
    """Broker adapter for Interactive Brokers.

    Connects to TWS or IB Gateway on the local host by default (port 7497 = paper TWS, 4002 =
    paper Gateway; 7496 = live TWS, 4001 = live Gateway). ``ib_insync`` is the runtime library
    — imported lazily so importing this module never fails on machines without it (a runtime
    ``BrokerError`` fires with an install hint on first use).
    """

    name = "interactive-brokers"

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 42,
        account: str = "",
        enable_live_orders: bool = False,
        readonly_market_data: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._account = account
        self._enable_live_orders = enable_live_orders
        self._readonly = readonly_market_data
        self._ib: Any = None                    # lazy-imported ib_insync.IB() instance
        self._connected = False

    # ---- connection lifecycle ------------------------------------------------

    def _require_ib(self) -> Any:
        """Lazy import + connect. Never raises during module import so tests don't need ib_insync."""
        if self._ib is not None and self._connected:
            return self._ib
        try:
            import ib_insync                    # noqa: F401 — imported for its side effect
        except ImportError as e:
            raise BrokerError(
                "IBBroker requires 'ib_insync' (`uv add ib_insync` or "
                "`pip install ib_insync`), plus a running TWS or IB Gateway with the API "
                "enabled on the configured host/port."
            ) from e
        from ib_insync import IB
        self._ib = IB()
        try:
            self._ib.connect(self._host, self._port, clientId=self._client_id,
                              readonly=self._readonly, timeout=15.0)
        except Exception as e:
            raise BrokerError(
                f"IBBroker: failed to connect to {self._host}:{self._port} "
                f"(clientId={self._client_id}). Is TWS / IB Gateway running with the API "
                f"enabled and the port matching (paper Gateway 4002 / paper TWS 7497 / "
                f"live Gateway 4001 / live TWS 7496)? Underlying error: {e}"
            ) from e
        self._connected = True
        log.info("ib.connected", host=self._host, port=self._port,
                 client_id=self._client_id, account=self._account)
        return self._ib

    def close(self) -> None:
        if self._ib is not None and self._connected:
            try:
                self._ib.disconnect()
            except Exception:                   # pragma: no cover — best-effort cleanup
                pass
        self._connected = False

    def __enter__(self) -> "IBBroker":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---- read-only surface ---------------------------------------------------

    def accounts(self) -> list[Account]:
        """One account row — either the account explicitly named at construction or the first
        managed account IB reports. Structural: keeps the Router's stable-account-id contract."""
        ib = self._require_ib()
        accs = list(ib.managedAccounts())
        if not accs:
            return []
        if self._account:
            if self._account not in accs:
                raise BrokerError(f"IBBroker: configured account {self._account!r} not in "
                                  f"managedAccounts {accs}")
            picked = self._account
        else:
            picked = accs[0]
        return [Account(type="IB", number=picked, status="Active", isPrimary=True)]

    def positions(self, account_number: str) -> list[Position]:
        ib = self._require_ib()
        raw = ib.positions(account=account_number)
        out: list[Position] = []
        for p in raw:
            out.append(Position(
                symbol=p.contract.symbol,
                symbolId=p.contract.conId or 0,
                openQuantity=float(p.position),
                averageEntryPrice=float(p.avgCost or 0.0),
                # currentPrice populated separately via reqMktData when the caller needs it.
                currentPrice=0.0,
                totalCost=float((p.avgCost or 0.0) * (p.position or 0.0)),
            ))
        return out

    def quote(self, symbol: str) -> Quote:
        return self.quotes([symbol])[0]

    def quotes(self, symbols: list[str]) -> list[Quote]:
        """Live tickers for one or more symbols. Uses SMART/USD stock as the default contract
        shape; call ``quote_contract`` for anything past a plain US equity."""
        ib = self._require_ib()
        from ib_insync import Stock              # local import so module import stays lightweight
        contracts = [Stock(s, "SMART", "USD") for s in symbols]
        tickers = ib.reqTickers(*contracts)
        out: list[Quote] = []
        for sym, t in zip(symbols, tickers, strict=True):
            out.append(Quote(
                symbol=sym, symbolId=int(t.contract.conId or 0),
                bidPrice=float(t.bid) if t.bid and t.bid > 0 else None,
                askPrice=float(t.ask) if t.ask and t.ask > 0 else None,
                lastTradePrice=float(t.last) if t.last and t.last > 0 else None,
            ))
        return out

    def quote_contract(self, contract: IBContract) -> Quote:
        """Full-fidelity quote for a specific IBContract — bonds, futures, options, forex."""
        ib = self._require_ib()
        c = _to_ib_contract(contract)
        ticker = ib.reqTickers(c)[0]
        return Quote(
            symbol=contract.symbol, symbolId=int(ticker.contract.conId or 0),
            bidPrice=float(ticker.bid) if ticker.bid and ticker.bid > 0 else None,
            askPrice=float(ticker.ask) if ticker.ask and ticker.ask > 0 else None,
            lastTradePrice=float(ticker.last) if ticker.last and ticker.last > 0 else None,
        )

    def candles(self, symbol: str, start: datetime, end: datetime,
                interval: str = "OneDay") -> list[Candle]:
        """Historical bars via reqHistoricalData. ``interval`` accepts the project's canonical
        strings; translates to IB's ``barSizeSetting`` at call time."""
        ib = self._require_ib()
        from ib_insync import Stock
        duration_days = max(1, (end - start).days)
        bar_size = _interval_to_ib_bar_size(interval)
        contract = Stock(symbol, "SMART", "USD")
        bars = ib.reqHistoricalData(
            contract, endDateTime=end.strftime("%Y%m%d %H:%M:%S"),
            durationStr=f"{duration_days} D", barSizeSetting=bar_size,
            whatToShow="TRADES", useRTH=True, formatDate=1,
        )
        out: list[Candle] = []
        for b in bars:
            ts = b.date if isinstance(b.date, datetime) else datetime.combine(b.date, datetime.min.time())
            out.append(Candle(
                start=ts, end=ts, open=float(b.open), high=float(b.high),
                low=float(b.low), close=float(b.close), volume=int(b.volume),
            ))
        return out

    def equity(self, account_number: str, currency: str = "USD") -> float:
        """NetLiquidation from accountValues, filtered by requested currency."""
        ib = self._require_ib()
        vals = ib.accountValues(account_number)
        for v in vals:
            if v.tag == "NetLiquidation" and v.currency == currency:
                try:
                    return float(v.value)
                except ValueError:
                    return 0.0
        return 0.0

    # ---- L2 depth-of-book ----------------------------------------------------

    def request_l2_book(self, contract: IBContract, num_rows: int = 10) -> L2Book:
        """Snapshot of the top-N bids/asks per exchange. Requires an IB market data subscription
        that includes deep book access (varies by exchange). Emitted as an ``L2Book`` for
        research consumers; not part of the ``Broker`` protocol.
        """
        ib = self._require_ib()
        c = _to_ib_contract(contract)
        ticker = ib.reqMktDepth(c, numRows=num_rows, isSmartDepth=(contract.exchange == "SMART"))
        # ib_insync buffers depth into ticker.domBids/domAsks; wait briefly for the first fill.
        ib.sleep(1.0)
        bids = tuple(L2Level(price=float(l.price), size=float(l.size),
                              exchange=l.exchange or "", market_maker=l.marketMaker or "")
                     for l in (ticker.domBids or []))
        asks = tuple(L2Level(price=float(l.price), size=float(l.size),
                              exchange=l.exchange or "", market_maker=l.marketMaker or "")
                     for l in (ticker.domAsks or []))
        ib.cancelMktDepth(c, isSmartDepth=(contract.exchange == "SMART"))
        return L2Book(symbol=contract.symbol, as_of=datetime.utcnow(),
                       bids=bids, asks=asks)

    # ---- order surface -------------------------------------------------------

    def place_order(self, order: Order) -> Order:
        """Refuses unless ``enable_live_orders=True`` at construction. Same contract as
        ``KrakenBroker``. Wraps the project's ``Order`` in IB's Order + Contract shape."""
        if not self._enable_live_orders:
            raise OrderRejected(
                "IBBroker: live orders disabled. Wrap this broker in PaperBroker(feed=...) "
                "for simulated fills, or construct with enable_live_orders=True after the human "
                "go-live confirmation."
            )
        ib = self._require_ib()
        from ib_insync import LimitOrder, MarketOrder, Stock

        contract = Stock(order.symbol, "SMART", "USD")
        if order.orderType.value == "Limit" and order.limitPrice is not None:
            ib_order = LimitOrder(
                action="BUY" if order.action == OrderAction.BUY else "SELL",
                totalQuantity=order.totalQuantity,
                lmtPrice=order.limitPrice,
            )
        else:
            ib_order = MarketOrder(
                action="BUY" if order.action == OrderAction.BUY else "SELL",
                totalQuantity=order.totalQuantity,
            )
        if self._account:
            ib_order.account = self._account
        trade = ib.placeOrder(contract, ib_order)
        order.id = int(trade.order.orderId or 0)
        log.info("ib.order.submitted",
                 symbol=order.symbol, qty=order.totalQuantity,
                 action=order.action.value, order_id=order.id)
        return order

    def place_algo_order(self, contract: IBContract, action: str, quantity: float, *,
                          algo: IBAlgoOrderType, limit_price: float | None = None,
                          tif: IBTimeInForce = "DAY",
                          algo_params: dict[str, Any] | None = None) -> int:
        """Place an IB algo / HFT-adjacent order. Gated the same as ``place_order``.

        ``algo_params`` maps to IB's ``algoParams`` list (e.g. ``{"startTime": "20261231-14:30:00
        US/Eastern", "endTime": "20261231-16:00:00 US/Eastern", "allowPastEndTime": False}`` for
        VWAP). Returns the IB order id.
        """
        if not self._enable_live_orders:
            raise OrderRejected("IBBroker: live orders disabled; enable_live_orders=True required.")
        ib = self._require_ib()
        from ib_insync import Order as IBOrder
        c = _to_ib_contract(contract)
        ib_order = IBOrder()
        ib_order.action = action.upper()
        ib_order.totalQuantity = float(quantity)
        ib_order.orderType = algo
        ib_order.tif = tif
        if limit_price is not None:
            ib_order.lmtPrice = float(limit_price)
        if algo in ("VWAP", "TWAP"):
            ib_order.algoStrategy = algo
            ib_order.algoParams = [
                {"tag": k, "value": str(v)} for k, v in (algo_params or {}).items()
            ]
        if self._account:
            ib_order.account = self._account
        trade = ib.placeOrder(c, ib_order)
        oid = int(trade.order.orderId or 0)
        log.info("ib.algo.submitted", symbol=contract.symbol, algo=algo,
                 qty=quantity, action=action, order_id=oid, tif=tif)
        return oid

    def cancel_order(self, account_number: str, order_id: int) -> None:
        if not self._enable_live_orders:
            raise BrokerError("IBBroker: live orders disabled; nothing to cancel.")
        ib = self._require_ib()
        for trade in ib.trades():
            if int(trade.order.orderId or 0) == int(order_id):
                ib.cancelOrder(trade.order)
                return
        raise BrokerError(f"IBBroker: order {order_id} not found in open trades.")


# ---- helpers -----------------------------------------------------------------


def _to_ib_contract(spec: IBContract) -> Any:
    """Convert the project's IBContract into an ib_insync Contract instance."""
    from ib_insync import (
        Bond,
        Contract,
        ContFuture,
        Crypto,
        Forex,
        Future,
        Index,
        MutualFund,
        Option,
        Stock,
    )
    if spec.sec_type == "stock":
        return Stock(spec.symbol, spec.exchange, spec.currency,
                      primaryExchange=spec.primary_exchange)
    if spec.sec_type == "future":
        # IBContract's exchange defaults to "SMART" which is meaningful for stocks but wrong
        # for futures — SMART routing doesn't handle CME/GLOBEX/CBOT/NYMEX. Swap the default
        # sentinel to GLOBEX; a caller who explicitly passes something else keeps it.
        futures_exchange = "GLOBEX" if spec.exchange in ("", "SMART") else spec.exchange
        if spec.expiry:
            return Future(spec.symbol, spec.expiry, futures_exchange,
                            currency=spec.currency, multiplier=spec.multiplier,
                            tradingClass=spec.trading_class)
        # Continuous-contract front-month; IB resolves to the current lead expiry.
        return ContFuture(spec.symbol, futures_exchange,
                             currency=spec.currency, multiplier=spec.multiplier,
                             tradingClass=spec.trading_class)
    if spec.sec_type == "option":
        return Option(spec.symbol, spec.expiry, spec.strike or 0.0,
                       spec.right or "C", spec.exchange or "SMART",
                       currency=spec.currency, multiplier=spec.multiplier or "100")
    if spec.sec_type == "bond":
        return Bond(symbol=spec.symbol, exchange=spec.exchange or "SMART",
                     currency=spec.currency)
    if spec.sec_type == "forex":
        return Forex(pair=spec.symbol)               # e.g. "EURUSD"
    if spec.sec_type == "index":
        return Index(spec.symbol, spec.exchange or "CBOE", spec.currency)
    if spec.sec_type == "fund":
        return MutualFund(spec.symbol, spec.exchange or "FUNDSERV", spec.currency)
    if spec.sec_type == "crypto":
        return Crypto(spec.symbol, spec.exchange or "PAXOS", spec.currency)
    # Escape hatch: raw Contract for any type not enumerated above.
    return Contract(secType=spec.sec_type.upper(), symbol=spec.symbol,
                     exchange=spec.exchange, currency=spec.currency)


def _interval_to_ib_bar_size(interval: str) -> str:
    """Project canonical interval → IB bar-size string."""
    return {
        "OneMinute": "1 min",
        "FiveMinutes": "5 mins",
        "FifteenMinutes": "15 mins",
        "ThirtyMinutes": "30 mins",
        "OneHour": "1 hour",
        "FourHours": "4 hours",
        "OneDay": "1 day",
        "OneWeek": "1 week",
    }.get(interval, "1 day")
