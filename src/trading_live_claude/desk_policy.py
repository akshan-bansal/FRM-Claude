"""Desk venue policy: which instrument class trades on which broker.

The desk runs three separate books, one broker each:

  * **Equities** on Questrade.
  * **Crypto** ("currencies") on Kraken.
  * **Futures and commodities** on Interactive Brokers.

So the Interactive Brokers path carries futures, plus (since 2026-09-21) equities listed on
exchanges Questrade can't reach — LSE, ASX, Tokyo, Hong Kong — for exchange hopping outside
Canadian hours. North American equities (US, TSX, TSX-V) stay on Questrade and are refused here.
FX is still not sourced from IB: a basket spanning more than one currency is refused, so the
hopping runs as one book per currency (a GBP book for LSE, a JPY book for Tokyo + OSE futures, …)
rather than one commingled book normalized through spot FX.
"""
from __future__ import annotations

from .venues import currency_of, venue_for

# Equity venues Questrade covers. Their stocks trade there, never through IB.
QUESTRADE_EQUITY_VENUES = frozenset({"US", "TSX", "TSXV"})


class VenuePolicyError(ValueError):
    """An instrument was routed to a broker the desk policy does not allow for its class."""


def is_future(symbol: str) -> bool:
    return symbol.startswith("/")


def is_crypto(symbol: str) -> bool:
    return "/" in symbol and not symbol.startswith("/")


def is_equity(symbol: str) -> bool:
    return not is_future(symbol) and not is_crypto(symbol)


def assert_ib_futures_only(symbols: list[str]) -> None:
    """Refuse any equity handed to the IB path — equities trade on Questrade, not IB."""
    equities = sorted(s for s in symbols if is_equity(s))
    if equities:
        raise VenuePolicyError(
            f"Equities trade on Questrade, not IB (desk venue split): {', '.join(equities)}. "
            f"The IB book carries futures/commodities only. Route these through the Questrade "
            f"equity monitor instead."
        )


def assert_ib_no_questrade_equities(symbols: list[str]) -> None:
    """Refuse North American equities on IB; futures and overseas-listed equities pass."""
    domestic = sorted(s for s in symbols
                      if is_equity(s) and venue_for(s)[0].code in QUESTRADE_EQUITY_VENUES)
    if domestic:
        raise VenuePolicyError(
            f"US/TSX equities trade on Questrade, not IB (desk venue split): {', '.join(domestic)}. "
            f"IB carries futures and overseas listings (.L, .AX, .T, .HK) only."
        )


def require_explicit_book_sizing(numeraire: str, paper_equity: float | None,
                                 min_ticket: float | None) -> None:
    """A non-CAD/USD book must state its equity and min ticket in its own currency.

    The defaults (100,000 equity, 100 min ticket) are CAD/USD-sized. Read as yen they would be a
    ~US$650 book with a ~US$0.65 min ticket, which silently disables the min-ticket gate. Fail
    closed instead of guessing an FX rate.
    """
    if numeraire.upper() in ("CAD", "USD"):
        return
    missing = [flag for flag, v in (("--paper-equity", paper_equity), ("--min-ticket", min_ticket))
               if v is None]
    if missing:
        raise VenuePolicyError(
            f"A {numeraire.upper()} book needs {' and '.join(missing)} set explicitly in "
            f"{numeraire.upper()}; the defaults are CAD/USD-sized."
        )


def assert_single_currency(symbols: list[str], numeraire: str) -> None:
    """Refuse a basket that would need FX to normalize — FX is no longer sourced from IB.

    A single-currency book needs no conversion. A mixed-currency basket used to be triangulated
    through IB spot pairs; with FX pulled off IB there is no rate source, so run one book per
    currency (``--account-currency`` matching the contracts) instead of commingling them.
    """
    numeraire = numeraire.upper()
    foreign = sorted({currency_of(s) for s in symbols} - {numeraire})
    if foreign:
        raise VenuePolicyError(
            f"Basket spans {', '.join([numeraire, *foreign])}; FX conversion was pulled off IB, so "
            f"there is no rate source to normalize into {numeraire}. Run one book per currency: set "
            f"--account-currency to the contracts' currency (e.g. USD for CME micros), or drop the "
            f"non-{numeraire} names."
        )
