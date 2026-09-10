"""IBBroker tests. Covers what can be exercised WITHOUT a running TWS/IB Gateway or
ib_insync installed — the module-level contract, the live-order gate, and the IBContract
translation to ib_insync shape when the library is (mock-)available.

Live-connection paths and market-data calls are deliberately NOT tested here — they need a
real TWS instance and IB market-data subscriptions; those go in an integration test suite.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from trading_live_claude.brokers.base import BrokerError, OrderRejected
from trading_live_claude.brokers.ib import (
    IBBroker,
    IBContract,
    _infer_stock_venue,
    _interval_to_ib_bar_size,
    _to_ib_contract,
)
from trading_live_claude.brokers.models import (
    Order,
    OrderAction,
    OrderType,
)


def _order(sym: str = "AAPL", qty: int = 10) -> Order:
    return Order(symbol=sym, symbolId=0, action=OrderAction.BUY,
                 orderType=OrderType.MARKET, totalQuantity=qty)


def test_place_order_refuses_loudly_by_default() -> None:
    """The go-live gate: never send unless enable_live_orders=True at construction."""
    b = IBBroker()
    with pytest.raises(OrderRejected, match="live orders disabled"):
        b.place_order(_order())


def test_cancel_order_is_gated_off_by_default() -> None:
    b = IBBroker()
    with pytest.raises(BrokerError, match="disabled"):
        b.cancel_order("DU12345", 1)


def test_place_algo_order_refuses_loudly_by_default() -> None:
    """Algo orders share the same gate as the plain order path — no VWAP unless enabled."""
    b = IBBroker()
    with pytest.raises(OrderRejected, match="live orders disabled"):
        b.place_algo_order(IBContract(symbol="AAPL"), "BUY", 100, algo="VWAP")


def test_missing_ib_insync_raises_actionable_broker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ib_insync is not installed, first use gives an install hint — not an ImportError."""
    monkeypatch.setitem(sys.modules, "ib_insync", None)
    b = IBBroker(enable_live_orders=True)
    with pytest.raises(BrokerError, match="requires 'ib_insync'"):
        b.place_order(_order())


def test_interval_bar_size_mapping() -> None:
    assert _interval_to_ib_bar_size("OneDay") == "1 day"
    assert _interval_to_ib_bar_size("OneHour") == "1 hour"
    assert _interval_to_ib_bar_size("FiveMinutes") == "5 mins"
    # Unknown intervals default to daily rather than raising.
    assert _interval_to_ib_bar_size("garbage") == "1 day"


def test_contract_translation_selects_the_right_ib_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """_to_ib_contract routes each sec_type to its matching ib_insync class."""
    # Fake ib_insync module with sentinel classes so we can assert the SHAPE of the mapping
    # without needing the real library installed.
    fake = SimpleNamespace(
        Stock=lambda *a, **k: ("Stock", a, k),
        Future=lambda *a, **k: ("Future", a, k),
        ContFuture=lambda *a, **k: ("ContFuture", a, k),
        Option=lambda *a, **k: ("Option", a, k),
        Bond=lambda **k: ("Bond", k),
        Forex=lambda **k: ("Forex", k),
        Index=lambda *a, **k: ("Index", a, k),
        MutualFund=lambda *a, **k: ("MutualFund", a, k),
        Crypto=lambda *a, **k: ("Crypto", a, k),
        Contract=lambda **k: ("Contract", k),
    )
    monkeypatch.setitem(sys.modules, "ib_insync", fake)

    stock = _to_ib_contract(IBContract(symbol="AAPL"))
    assert stock[0] == "Stock"

    # Front-month continuous future when no expiry given. Exchange default swaps SMART→GLOBEX
    # for futures because IB's SMART routing doesn't handle CME/GLOBEX/CBOT/NYMEX.
    cf = _to_ib_contract(IBContract(symbol="ES", sec_type="future"))
    assert cf[0] == "ContFuture"
    assert "GLOBEX" in cf[1]                            # positional arg 2 = exchange

    # Specific expiry → Future
    dated = _to_ib_contract(IBContract(symbol="ES", sec_type="future", expiry="20261231"))
    assert dated[0] == "Future"
    assert "GLOBEX" in dated[1]

    # Explicit exchange override — GLOBEX default is a fallback, not a hard-code
    cme = _to_ib_contract(IBContract(symbol="ES", sec_type="future", exchange="CME"))
    assert "CME" in cme[1]

    opt = _to_ib_contract(IBContract(symbol="SPY", sec_type="option",
                                       expiry="20261219", strike=500.0, right="C"))
    assert opt[0] == "Option"

    bond = _to_ib_contract(IBContract(symbol="US91282CJU63", sec_type="bond"))
    assert bond[0] == "Bond"

    fx = _to_ib_contract(IBContract(symbol="EURUSD", sec_type="forex"))
    assert fx[0] == "Forex"

    idx = _to_ib_contract(IBContract(symbol="SPX", sec_type="index"))
    assert idx[0] == "Index"

    fund = _to_ib_contract(IBContract(symbol="VFIAX", sec_type="fund"))
    assert fund[0] == "MutualFund"

    crypto = _to_ib_contract(IBContract(symbol="BTC", sec_type="crypto"))
    assert crypto[0] == "Crypto"


# ---- 2026-09-09: stock exchange + currency inference for socket path ------------------

def test_infer_stock_venue_us_default() -> None:
    """US-listed tickers with no suffix route SMART/USD (pre-2026-09-09 blanket default)."""
    assert _infer_stock_venue("AAPL") == ("AAPL", "SMART", "USD")
    assert _infer_stock_venue("SPY") == ("SPY", "SMART", "USD")


def test_infer_stock_venue_tsx_suffix() -> None:
    """.TO tickers route TSE/CAD; suffix stripped from bare symbol handed to Stock()."""
    assert _infer_stock_venue("XIC.TO") == ("XIC", "TSE", "CAD")
    assert _infer_stock_venue("VDY.TO") == ("VDY", "TSE", "CAD")
    assert _infer_stock_venue("SRU.UN.TO") == ("SRU.UN", "TSE", "CAD")


def test_infer_stock_venue_other_suffixes() -> None:
    """Symmetric routing for London / Sydney / TSX Venture."""
    assert _infer_stock_venue("VOD.L") == ("VOD", "LSE", "GBP")
    assert _infer_stock_venue("BHP.AX") == ("BHP", "ASX", "AUD")
    assert _infer_stock_venue("XYZ.V") == ("XYZ", "VENTURE", "CAD")


def test_infer_stock_venue_case_insensitive_on_suffix() -> None:
    """Symbols may arrive in mixed case; suffix match must not depend on case, and the
    stripped bare symbol preserves the input case (broker-side normalizes it anyway)."""
    assert _infer_stock_venue("xic.to")[1:] == ("TSE", "CAD")
    assert _infer_stock_venue("VDY.To")[1:] == ("TSE", "CAD")


def test_place_order_routes_canadian_ticker_to_tse_cad(monkeypatch: pytest.MonkeyPatch) -> None:
    """place_order must construct Stock() with TSE/CAD for a .TO ticker, not SMART/USD.
    Closes the audit-gap where XIC.TO submitted via socket silently mis-routed."""
    captured = {}

    class _FakeStock:
        def __init__(self, symbol, exchange, currency):
            captured['symbol'] = symbol
            captured['exchange'] = exchange
            captured['currency'] = currency

    class _FakeOrder:
        def __init__(self, **kwargs):
            self.orderId = 42
            for k, v in kwargs.items():
                setattr(self, k, v)

    class _FakeTrade:
        def __init__(self, contract, order):
            self.order = order

    class _FakeIB:
        def placeOrder(self, contract, order):
            return _FakeTrade(contract, order)

    fake_module = SimpleNamespace(
        Stock=_FakeStock,
        MarketOrder=_FakeOrder,
        LimitOrder=_FakeOrder,
    )
    monkeypatch.setitem(sys.modules, "ib_insync", fake_module)

    b = IBBroker(enable_live_orders=True)
    monkeypatch.setattr(b, "_require_ib", lambda: _FakeIB())
    b.place_order(_order(sym="XIC.TO"))
    assert captured == {"symbol": "XIC", "exchange": "TSE", "currency": "CAD"}


def test_place_order_still_defaults_us_ticker_to_smart_usd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backwards compat: unsuffixed tickers keep the SMART/USD routing."""
    captured = {}

    class _FakeStock:
        def __init__(self, symbol, exchange, currency):
            captured.update(symbol=symbol, exchange=exchange, currency=currency)

    class _FakeOrder:
        def __init__(self, **kwargs):
            self.orderId = 1
            for k, v in kwargs.items(): setattr(self, k, v)

    class _FakeIB:
        def placeOrder(self, contract, order):
            return SimpleNamespace(order=order)

    monkeypatch.setitem(sys.modules, "ib_insync", SimpleNamespace(
        Stock=_FakeStock, MarketOrder=_FakeOrder, LimitOrder=_FakeOrder))
    b = IBBroker(enable_live_orders=True)
    monkeypatch.setattr(b, "_require_ib", lambda: _FakeIB())
    b.place_order(_order(sym="AAPL"))
    assert captured == {"symbol": "AAPL", "exchange": "SMART", "currency": "USD"}


def test_ib_broker_exports_from_package_namespace() -> None:
    """Registration check — IBBroker + its dataclasses must be importable from brokers/."""
    from trading_live_claude.brokers import (
        IBAssetClass,
        IBBroker as ExportedIB,
        IBContract as ExportedContract,
        L2Book,
        L2Level,
    )
    assert ExportedIB is IBBroker
    # IBAssetClass is a Literal so identity would fail — just check it's importable + non-empty.
    assert IBAssetClass is not None
    # Dataclasses have __dataclass_fields__.
    assert hasattr(ExportedContract, "__dataclass_fields__")
    assert hasattr(L2Book, "__dataclass_fields__")
    assert hasattr(L2Level, "__dataclass_fields__")


def test_close_is_a_no_op_when_never_connected() -> None:
    """close() must not fail on a broker that was constructed but never touched the network."""
    b = IBBroker()
    b.close()          # no exception
    with IBBroker() as b2:
        pass           # context-manager exit path also fine
