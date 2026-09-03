"""IBWebBroker tests. Mocks the Web API HTTP surface with respx — same shape as the
KrakenBroker tests. No live IB Gateway or OAuth exchange happens here.
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from trading_live_claude.brokers.base import BrokerError, OrderRejected
from trading_live_claude.brokers.ib_web import (
    CPGatewayAuth,
    IBWebBroker,
    OAuth2JWTAuth,
    _num,
)
from trading_live_claude.brokers.models import (
    Order,
    OrderAction,
    OrderType,
    TimeInForce,
)


_CP_BASE = "https://localhost:5000/v1/api"


def _mk_broker(enable_live: bool = False) -> IBWebBroker:
    """CPGatewayAuth against the standard localhost:5000 base; respx mocks all HTTPS."""
    return IBWebBroker(auth=CPGatewayAuth(verify_ssl=False), enable_live_orders=enable_live)


# ---- session + auth ------------------------------------------------------------------------


def test_cp_gateway_auth_uses_localhost_5000_v1_api_base() -> None:
    """The retail Gateway path pins to https://localhost:5000/v1/api."""
    auth = CPGatewayAuth()
    assert auth.base_url == "https://localhost:5000/v1/api"


def test_oauth2_auth_defaults_point_at_the_public_api_host() -> None:
    """Institutional path hits api.ibkr.com/v1/api, not localhost."""
    auth = OAuth2JWTAuth(client_id="c", private_key_pem="", key_id="")
    assert auth.base_url == "https://api.ibkr.com/v1/api"


@respx.mock
def test_tickle_calls_the_keepalive_endpoint_on_cp_gateway() -> None:
    """CP Gateway session times out silently after idle — tickle is how you keep it warm."""
    route = respx.post(f"{_CP_BASE}/tickle").mock(return_value=httpx.Response(200, json={"ok": True}))
    b = _mk_broker()
    b.tickle()
    assert route.call_count == 1


def test_tickle_is_a_noop_for_oauth2_which_uses_token_refresh_instead() -> None:
    auth = OAuth2JWTAuth(client_id="c", private_key_pem="", key_id="")
    b = IBWebBroker(auth=auth)
    b.tickle()          # must not raise, must not call anything


# ---- contract resolution -------------------------------------------------------------------


@respx.mock
def test_resolve_conid_caches_after_the_first_lookup() -> None:
    route = respx.post(f"{_CP_BASE}/iserver/secdef/search").mock(
        return_value=httpx.Response(200, json=[{"conid": 265598, "symbol": "AAPL"}])
    )
    b = _mk_broker()
    assert b.resolve_conid("AAPL") == 265598
    assert b.resolve_conid("AAPL") == 265598           # cached — no second HTTP call
    assert route.call_count == 1


@respx.mock
def test_resolve_conid_raises_a_broker_error_when_no_match() -> None:
    respx.post(f"{_CP_BASE}/iserver/secdef/search").mock(
        return_value=httpx.Response(200, json=[])
    )
    with pytest.raises(BrokerError, match="no contracts found"):
        _mk_broker().resolve_conid("NOSUCH")


# ---- futures resolution ---------------------------------------------------------------------


@respx.mock
def test_set_sec_type_routes_futures_through_trsrv_futures_and_picks_front_month() -> None:
    """FUT-registered roots must resolve via /trsrv/futures — /iserver/secdef/search returns roots
    only, not per-expiration conids. Front-month = earliest expiration strictly after today.
    """
    # Three expirations: one already past, front-month, and one further out. Broker must pick the
    # middle one (earliest that is strictly after today).
    respx.get(f"{_CP_BASE}/trsrv/futures").mock(
        return_value=httpx.Response(200, json={
            "ES": [
                {"conid": 100, "expirationDate": "20200101"},        # past
                {"conid": 200, "expirationDate": "20991231"},        # far
                {"conid": 150, "expirationDate": "20990101"},        # front
            ]
        })
    )
    b = _mk_broker()
    b.set_sec_type("ES", "FUT")
    assert b.resolve_conid("ES") == 150


@respx.mock
def test_set_sec_type_falls_back_to_earliest_when_all_expirations_are_past() -> None:
    """A stale mid-roll response (every listed contract already expired) must still return a
    conid rather than raise — pick the earliest of the stale list and let the market-data call
    surface any real issue downstream.
    """
    respx.get(f"{_CP_BASE}/trsrv/futures").mock(
        return_value=httpx.Response(200, json={
            "GC": [
                {"conid": 700, "expirationDate": "20200101"},
                {"conid": 800, "expirationDate": "20210101"},
            ]
        })
    )
    b = _mk_broker()
    b.set_sec_type("GC", "FUT")
    assert b.resolve_conid("GC") == 700


@respx.mock
def test_futures_root_unknown_to_ib_raises_actionable_error() -> None:
    respx.get(f"{_CP_BASE}/trsrv/futures").mock(
        return_value=httpx.Response(200, json={"ZZZZ": []})
    )
    b = _mk_broker()
    b.set_sec_type("ZZZZ", "FUT")
    with pytest.raises(BrokerError, match="no futures listed for root"):
        b.resolve_conid("ZZZZ")


@respx.mock
def test_set_sec_type_invalidates_a_stale_stk_cache_entry_for_the_same_symbol() -> None:
    """Registering an override AFTER a symbol was already resolved as STK must clear the stale
    cache — otherwise the next call would still return the equity ticker's conid (Eversource for
    'ES', Colgate for 'CL') rather than the future.
    """
    respx.post(f"{_CP_BASE}/iserver/secdef/search").mock(
        return_value=httpx.Response(200, json=[{"conid": 999, "symbol": "ES"}])
    )
    fut_route = respx.get(f"{_CP_BASE}/trsrv/futures").mock(
        return_value=httpx.Response(200, json={
            "ES": [{"conid": 555, "expirationDate": "20990101"}]
        })
    )
    b = _mk_broker()
    assert b.resolve_conid("ES") == 999                    # first as STK
    b.set_sec_type("ES", "FUT")                            # override
    assert b.resolve_conid("ES") == 555                    # now as FUT
    assert fut_route.call_count == 1


def test_sec_type_default_is_still_stk_when_no_override_registered() -> None:
    """Backward compat: the resolve_conid default hasn't changed. Symbols without an override
    keep their historical STK resolution path."""
    b = _mk_broker()
    assert b._sec_type_for("AAPL") == "STK"
    b.set_sec_type("AAPL", "OPT")
    assert b._sec_type_for("AAPL") == "OPT"


# ---- accounts / positions / equity ---------------------------------------------------------


@respx.mock
def test_accounts_flags_the_selected_account_as_primary() -> None:
    respx.get(f"{_CP_BASE}/iserver/accounts").mock(
        return_value=httpx.Response(200, json={
            "accounts": ["DU111", "DU222"],
            "selectedAccount": "DU222",
        })
    )
    accs = _mk_broker().accounts()
    assert [a.number for a in accs] == ["DU111", "DU222"]
    assert [a.isPrimary for a in accs] == [False, True]


@respx.mock
def test_positions_translates_web_api_shape_to_the_broker_position_model() -> None:
    respx.get(f"{_CP_BASE}/portfolio/DU111/positions/0").mock(
        return_value=httpx.Response(200, json=[
            {"conid": 265598, "ticker": "AAPL", "contractDesc": "AAPL",
             "position": 100.0, "avgCost": 150.0, "mktPrice": 175.25},
            {"conid": 76792991, "ticker": "SPY", "contractDesc": "SPY",
             "position": 0.0, "avgCost": 0.0, "mktPrice": 0.0},        # zero-qty filtered out
        ])
    )
    positions = _mk_broker().positions("DU111")
    assert len(positions) == 1
    p = positions[0]
    assert p.symbol == "AAPL" and p.symbolId == 265598
    assert p.openQuantity == 100.0
    assert p.averageEntryPrice == 150.0
    assert p.currentPrice == 175.25


@respx.mock
def test_equity_reads_netliquidation_matching_the_requested_currency() -> None:
    respx.get(f"{_CP_BASE}/portfolio/DU111/summary").mock(
        return_value=httpx.Response(200, json={
            "netliquidation": {"amount": 250_000.75, "currency": "USD"},
        })
    )
    b = _mk_broker()
    assert b.equity("DU111", "USD") == 250_000.75
    # currency mismatch → 0.0 (honest — we don't know the FX rate to convert)
    assert b.equity("DU111", "CAD") == 0.0


# ---- market data ---------------------------------------------------------------------------


@respx.mock
def test_quote_returns_bid_ask_last_from_the_snapshot_field_ids() -> None:
    """Web API returns market-data via numeric field ids (31=last, 84=bid, 86=ask)."""
    respx.post(f"{_CP_BASE}/iserver/secdef/search").mock(
        return_value=httpx.Response(200, json=[{"conid": 265598, "symbol": "AAPL"}]))
    respx.get(f"{_CP_BASE}/iserver/marketdata/snapshot").mock(
        return_value=httpx.Response(200, json=[{
            "conid": 265598, "31": "175.5", "84": "175.4", "86": "175.6",
        }])
    )
    q = _mk_broker().quote("AAPL")
    assert q.symbol == "AAPL" and q.symbolId == 265598
    assert q.bidPrice == 175.4 and q.askPrice == 175.6 and q.lastTradePrice == 175.5


@respx.mock
def test_quote_handles_ib_price_prefix_chars_like_c_for_close() -> None:
    """IB sometimes prefixes prices with 'C' when the market is closed. Parser must strip it."""
    respx.post(f"{_CP_BASE}/iserver/secdef/search").mock(
        return_value=httpx.Response(200, json=[{"conid": 1, "symbol": "X"}]))
    respx.get(f"{_CP_BASE}/iserver/marketdata/snapshot").mock(
        return_value=httpx.Response(200, json=[{"conid": 1, "31": "C42.5", "84": "42.4", "86": "42.6"}]))
    q = _mk_broker().quote("X")
    assert q.lastTradePrice == 42.5


@respx.mock
def test_candles_maps_history_data_to_the_broker_candle_model() -> None:
    respx.post(f"{_CP_BASE}/iserver/secdef/search").mock(
        return_value=httpx.Response(200, json=[{"conid": 265598, "symbol": "AAPL"}]))
    respx.get(f"{_CP_BASE}/iserver/marketdata/history").mock(
        return_value=httpx.Response(200, json={
            "data": [
                {"t": 1_700_000_000_000, "o": 100.0, "h": 101.0, "l": 99.5, "c": 100.5, "v": 1_000_000},
                {"t": 1_700_086_400_000, "o": 100.5, "h": 102.0, "l": 100.0, "c": 101.75, "v": 900_000},
            ],
        })
    )
    got = _mk_broker().candles("AAPL",
                                 start=datetime(2023, 11, 14, tzinfo=UTC),
                                 end=datetime(2023, 11, 15, tzinfo=UTC),
                                 interval="OneDay")
    assert len(got) == 2
    assert got[0].open == 100.0 and got[1].close == 101.75


# ---- order surface -------------------------------------------------------------------------


def _order(qty: int = 10) -> Order:
    return Order(symbol="AAPL", symbolId=0, action=OrderAction.BUY,
                 orderType=OrderType.LIMIT, totalQuantity=qty,
                 limitPrice=100.0, accountId="DU111", timeInForce=TimeInForce.DAY)


def test_place_order_refuses_loudly_by_default() -> None:
    with pytest.raises(OrderRejected, match="live orders disabled"):
        _mk_broker().place_order(_order())


def test_place_order_needs_account_id_even_when_gate_open() -> None:
    """Explicit accountId is required — IB's endpoint is per-account."""
    b = _mk_broker(enable_live=True)
    bad = Order(symbol="AAPL", symbolId=0, action=OrderAction.BUY,
                orderType=OrderType.MARKET, totalQuantity=1)     # no accountId
    with pytest.raises(OrderRejected, match="accountId"):
        b.place_order(bad)


@respx.mock
def test_place_order_auto_confirms_the_precheck_prompt() -> None:
    """Web API returns a warning prompt on some orders; auto-confirm so paper flow doesn't hang."""
    respx.post(f"{_CP_BASE}/iserver/secdef/search").mock(
        return_value=httpx.Response(200, json=[{"conid": 265598, "symbol": "AAPL"}]))
    orders_route = respx.post(f"{_CP_BASE}/iserver/account/DU111/orders").mock(
        return_value=httpx.Response(200, json=[
            {"id": "prompt-id-1", "message": ["The following order..."]},
        ])
    )
    confirm_route = respx.post(f"{_CP_BASE}/iserver/reply/prompt-id-1").mock(
        return_value=httpx.Response(200, json=[
            {"order_id": "917531", "order_status": "PreSubmitted"},
        ])
    )
    filled = _mk_broker(enable_live=True).place_order(_order())
    assert orders_route.call_count == 1
    assert confirm_route.call_count == 1
    assert filled.id == 917531


def test_cancel_order_is_gated_off_by_default() -> None:
    with pytest.raises(BrokerError, match="disabled"):
        _mk_broker().cancel_order("DU111", 12345)


# ---- module-level ---------------------------------------------------------------------------


def test_num_parses_ib_prefix_chars_and_bad_values() -> None:
    assert _num("100.5") == 100.5
    assert _num("C100.5") == 100.5        # closing-price marker
    assert _num("") is None
    assert _num(None) is None
    assert _num("garbage") is None
    assert _num(42) == 42.0


def test_ib_web_broker_exports_from_package_namespace() -> None:
    from trading_live_claude.brokers import (
        CPGatewayAuth as ExportedCP,
        IBWebAuth,
        IBWebBroker as ExportedBroker,
        OAuth2JWTAuth as ExportedOAuth,
    )
    assert ExportedBroker is IBWebBroker
    assert ExportedCP is CPGatewayAuth
    assert ExportedOAuth is OAuth2JWTAuth
    assert IBWebAuth is not None
