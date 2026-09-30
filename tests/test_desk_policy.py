from __future__ import annotations

import pytest

from trading_live_claude.desk_policy import (
    VenuePolicyError,
    assert_ib_futures_only,
    assert_ib_no_questrade_equities,
    assert_single_currency,
    is_crypto,
    is_equity,
    is_future,
    require_explicit_book_sizing,
)


def test_classification() -> None:
    assert is_future("/MCL") and not is_equity("/MCL") and not is_crypto("/MCL")
    assert is_crypto("BTC/USD") and not is_equity("BTC/USD")
    assert is_equity("AAPL") and is_equity("XIC.TO") and is_equity("7203.T")


def test_ib_refuses_equities_domestic_and_foreign() -> None:
    with pytest.raises(VenuePolicyError, match="Questrade"):
        assert_ib_futures_only(["/CL", "AAPL"])
    with pytest.raises(VenuePolicyError, match=r"7203\.T"):
        assert_ib_futures_only(["7203.T"])
    # A pure futures/crypto basket is allowed through.
    assert_ib_futures_only(["/CL", "/GC"])       # a pure futures/crypto basket is allowed


def test_single_currency_guard_refuses_a_mixed_basket() -> None:
    # Crypto BTC/USD is USD; a CAD numeraire needs FX, which is no longer on IB.
    with pytest.raises(VenuePolicyError, match="FX conversion was pulled off IB"):
        assert_single_currency(["BTC/USD"], "CAD")
    assert_single_currency(["BTC/USD", "ETH/USD"], "USD")   # USD numeraire matches, no FX needed


def test_ib_hopping_allows_overseas_listings_only() -> None:
    # Overseas listings and futures pass: these are what IB is for.
    assert_ib_no_questrade_equities(["7203.T", "ISF.L", "IOZ.AX", "2800.HK", "/MCL"])
    # US and TSX/TSX-V equities belong to Questrade.
    for sym in ("AAPL", "XIC.TO", "ABC.V"):
        with pytest.raises(VenuePolicyError, match="Questrade"):
            assert_ib_no_questrade_equities(["7203.T", sym])


def test_overseas_book_still_needs_a_single_currency() -> None:
    # Hopping is one book per currency, so a GBP + JPY basket is still refused.
    with pytest.raises(VenuePolicyError, match="one book per currency"):
        assert_single_currency(["ISF.L", "7203.T"], "GBP")
    assert_single_currency(["7203.T", "8306.T"], "JPY")


def test_non_cad_usd_book_must_size_itself_explicitly() -> None:
    require_explicit_book_sizing("USD", None, None)         # defaults are USD/CAD-sized
    require_explicit_book_sizing("cad", None, None)
    with pytest.raises(VenuePolicyError, match="--paper-equity and --min-ticket"):
        require_explicit_book_sizing("JPY", None, None)
    with pytest.raises(VenuePolicyError, match="--min-ticket"):
        require_explicit_book_sizing("GBP", 75_000.0, None)
    require_explicit_book_sizing("JPY", 15_000_000.0, 15_000.0)
