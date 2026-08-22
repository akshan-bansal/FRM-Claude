from __future__ import annotations

import pytest

from trading_live_claude.execution.asset_router import (
    DEFAULT_ASSET_BROKERAGE,
    AssetRouter,
)
from trading_live_claude.integrations.lean_algorithm import (
    DEFAULT_LEAN_ALGORITHM,
    render_lean_algorithm,
)


def test_default_routing_per_asset_class() -> None:
    r = AssetRouter()
    assert r.route("equity").brokerage == DEFAULT_ASSET_BROKERAGE["equity"]
    crypto = r.route("crypto")
    assert crypto.brokerage == "CoinbaseBrokerage"
    assert crypto.add_method == "AddCrypto"
    assert crypto.market == "Market.Coinbase"


def test_equity_and_future_add_methods() -> None:
    r = AssetRouter()
    assert r.route("equity").add_method == "AddEquity"
    assert r.route("future").add_method == "AddFuture"
    assert r.route("commodity").add_method == "AddFuture"


def test_override_mapping() -> None:
    r = AssetRouter({"crypto": "BinanceBrokerage"})
    assert r.route("crypto").brokerage == "BinanceBrokerage"
    assert r.route("equity").brokerage == DEFAULT_ASSET_BROKERAGE["equity"]  # untouched


def test_unknown_override_key_rejected() -> None:
    with pytest.raises(ValueError):
        AssetRouter({"forex": "OandaBrokerage"})


def test_unsupported_asset_class_route_raises() -> None:
    with pytest.raises(KeyError):
        AssetRouter().route("forex")


def test_generator_equity() -> None:
    src = render_lean_algorithm(symbol="SPY", add_method="AddEquity")
    assert 'self.AddEquity("SPY", Resolution.Daily).Symbol' in src
    assert "class GeneratedEmaCross(QCAlgorithm)" in src


def test_generator_crypto_includes_market() -> None:
    src = render_lean_algorithm(symbol="BTCUSD", add_method="AddCrypto", market="Market.Coinbase")
    assert 'self.AddCrypto("BTCUSD", Resolution.Daily, Market.Coinbase).Symbol' in src


def test_generator_dates_and_cash_parameterized() -> None:
    src = render_lean_algorithm(symbol="ES", add_method="AddFuture", start=(2020, 6, 1), cash=50000)
    assert "self.SetStartDate(2020, 6, 1)" in src
    assert "self.SetCash(50000)" in src
    assert 'self.AddFuture("ES", Resolution.Daily).Symbol' in src


def test_default_algorithm_is_equity_spy() -> None:
    assert 'self.AddEquity("SPY", Resolution.Daily).Symbol' in DEFAULT_LEAN_ALGORITHM
