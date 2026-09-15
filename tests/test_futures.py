from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_live_claude.brokers.base import StaleQuote
from trading_live_claude.brokers.fx import CurrencyNormalizingBroker, FxRates
from trading_live_claude.brokers.models import Candle, Order, OrderAction, OrderType, Quote
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.execution.router import Router
from trading_live_claude.execution.scheduler import SessionRouter
from trading_live_claude.futures import (
    FuturesBook,
    FuturesContract,
    make_roller,
    parse_ib_hours,
    spec_from_ib_details,
)
from trading_live_claude.intel.routing import classify_symbol
from trading_live_claude.venues import currency_of, market_open, venue_for


def _detail(con_id: int, local: str, expiry: str, month: str, *, symbol: str = "COIL",
            exchange: str = "IPE", currency: str = "USD", multiplier: str = "1000",
            magnifier: int = 1, tz: str = "GB-Eire", trading_class: str | None = None,
            hours: str = "20260914:0100-20260914:2300;20260915:0100-20260915:2300;"
                         "20260924:0100-20260924:2300") -> SimpleNamespace:
    return SimpleNamespace(
        contract=SimpleNamespace(conId=con_id, localSymbol=local, symbol=symbol, exchange=exchange,
                                 currency=currency, multiplier=multiplier, tradingClass=trading_class or symbol,
                                 lastTradeDateOrContractMonth=expiry),
        realExpirationDate=expiry, contractMonth=month, priceMagnifier=magnifier,
        timeZoneId=tz, liquidHours=hours, longName="Brent Crude")


BRENT = [_detail(2, "COILZ6", "20261030", "202612"), _detail(1, "COILX6", "20260930", "202611")]


def test_parse_ib_hours_both_formats_and_closed_days() -> None:
    new = parse_ib_hours("20260913:CLOSED;20260914:1700-20260915:1600", "US/Central")
    assert new == ((datetime(2026, 9, 14, 22, 0, tzinfo=UTC), datetime(2026, 9, 15, 21, 0, tzinfo=UTC)),)
    old = parse_ib_hours("20260914:0900-1130,1230-1530", "Japan")
    assert old == ((datetime(2026, 9, 14, 0, 0, tzinfo=UTC), datetime(2026, 9, 14, 2, 30, tzinfo=UTC)),
                   (datetime(2026, 9, 14, 3, 30, tzinfo=UTC), datetime(2026, 9, 14, 6, 30, tzinfo=UTC)))


def test_roll_date_stays_ahead_of_first_notice_style_deadlines() -> None:
    gold_dec = FuturesContract(1, "GCZ6", last_trade=date(2026, 12, 29), contract_month=date(2026, 12, 1))
    assert gold_dec.roll_date(5) == date(2026, 11, 24)       # before Nov 30 first notice
    crude_dec = FuturesContract(2, "CLZ6", last_trade=date(2026, 11, 19), contract_month=date(2026, 12, 1))
    assert crude_dec.roll_date(5) == date(2026, 11, 12)      # 5 business days before last trade


def test_spec_from_ib_details_sorts_contracts_and_scales_by_magnifier() -> None:
    spec = spec_from_ib_details(BRENT)
    assert spec is not None
    assert (spec.symbol, spec.exchange, spec.currency, spec.scale) == ("/COIL", "IPE", "USD", 1000.0)
    assert [c.local_symbol for c in spec.contracts] == ["COILX6", "COILZ6"]
    grains = spec_from_ib_details([_detail(9, "ZCZ6", "20261214", "202612", symbol="ZC",
                                           exchange="CBOT", multiplier="5000", magnifier=100)])
    assert grains is not None and grains.scale == 50.0       # cents -> dollars


def test_book_registers_venue_and_picks_the_contract_before_its_roll() -> None:
    book = FuturesBook(roll_bdays=5, clock=lambda: datetime(2026, 9, 14, 12, tzinfo=UTC))
    book.add(spec_from_ib_details(BRENT))
    assert book.contract_for("/COIL") == (1, "COIL", "IPE", "USD", "COIL")
    assert venue_for("/COIL")[0].code == "FUT" and currency_of("/COIL") == "USD"
    assert market_open("/COIL", datetime(2026, 9, 14, 12, tzinfo=UTC))
    assert not market_open("/COIL", datetime(2026, 9, 14, 23, 30, tzinfo=UTC))
    assert classify_symbol("/COIL") == "future"
    book.clock = lambda: datetime(2026, 9, 24, tzinfo=UTC)   # within 5 bdays of Sep 30
    assert book.pending_rolls()["/COIL"].local_symbol == "COILZ6"


def test_spec_keeps_one_trading_class_so_contract_sizes_never_mix() -> None:
    rows = [_detail(1, "SIZ6", "20261229", "202612", symbol="SI", exchange="COMEX", multiplier="5000"),
            _detail(2, "SILZ6", "20261229", "202612", symbol="SI", exchange="COMEX", multiplier="1000",
                    trading_class="SIL"),
            _detail(3, "SIH7", "20270329", "202703", symbol="SI", exchange="COMEX", multiplier="5000")]
    spec = spec_from_ib_details(rows)
    assert spec is not None and spec.trading_class == "SI" and spec.multiplier == 5000.0
    assert [c.local_symbol for c in spec.contracts] == ["SIZ6", "SIH7"]
    micro = spec_from_ib_details(rows, symbol="/SIL", trading_class="SIL")
    assert micro is not None and micro.multiplier == 1000.0 and len(micro.contracts) == 1


def test_unregistered_future_never_trades() -> None:
    assert not market_open("/NOPE", datetime(2026, 9, 14, 15, tzinfo=UTC))
    assert venue_for("/NOPE")[0].code == "FUT"


class _Feed:
    name = "ib"
    venue = "ib"

    def __init__(self, book: FuturesBook) -> None:
        self.book = book
        self.prices = {1: (70.0, 70.02), 2: (71.0, 71.02)}

    def quote(self, symbol: str) -> Quote:
        con_id = self.book.contract_for(symbol)[0]
        bid, ask = self.prices[con_id]
        return Quote(symbol=symbol, symbolId=con_id, bidPrice=bid, askPrice=ask, lastTradePrice=bid)

    def quotes(self, symbols):
        return [self.quote(s) for s in symbols]

    def candles(self, *a, **k):
        return [Candle(start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 9, 1, tzinfo=UTC),
                       open=70, high=71, low=69, close=70.5)]

    def accounts(self): return []
    def positions(self, _): return []
    def equity(self, *a, **k): return 0.0
    def place_order(self, order): return order
    def cancel_order(self, *a, **k): pass


def _cad_rates() -> FxRates:
    return FxRates(lambda pair: {"USDCAD": 1.37}.get(pair), "CAD")


def test_contract_multiplier_and_fx_scale_prices_to_one_contract_in_cad() -> None:
    book = FuturesBook(clock=lambda: datetime(2026, 9, 14, 12, tzinfo=UTC))
    book.add(spec_from_ib_details(BRENT))
    feed = CurrencyNormalizingBroker(_Feed(book), _cad_rates(), multiplier_for=book.multiplier_for)
    assert feed.quote("/COIL").bidPrice == pytest.approx(70.0 * 1000 * 1.37)
    (bar,) = feed.candles("/COIL", datetime.now(UTC), datetime.now(UTC))
    assert bar.close == pytest.approx(70.5 * 1000 * 1.37)


def test_paper_pnl_for_one_contract_uses_full_notional(tmp_path: Path) -> None:
    book = FuturesBook(clock=lambda: datetime(2026, 9, 14, 12, tzinfo=UTC))
    book.add(spec_from_ib_details(BRENT))
    raw = _Feed(book)
    paper = PaperBroker(feed=CurrencyNormalizingBroker(raw, FxRates(lambda p: None, "USD"),
                                                       multiplier_for=book.multiplier_for),
                        slippage_bps=0.0, commission_per_trade=0.0, journal_dir=tmp_path)
    paper.place_order(Order(symbol="/COIL", symbolId=1, action=OrderAction.BUY,
                            orderType=OrderType.MARKET, totalQuantity=1))
    raw.prices[1] = (71.0, 71.0)
    paper.mark_to_market()
    assert paper.equity("PAPER-001") - 100_000.0 == pytest.approx((71.0 - 70.01) * 1000)


def _roll_setup(tmp_path: Path, equity: float):
    now = [datetime(2026, 9, 14, 12, tzinfo=UTC)]
    book = FuturesBook(roll_bdays=5, clock=lambda: now[0])
    book.add(spec_from_ib_details(BRENT, symbol="/COIL"))
    raw = _Feed(book)
    paper = PaperBroker(feed=CurrencyNormalizingBroker(raw, FxRates(lambda p: None, "USD"),
                                                       multiplier_for=book.multiplier_for),
                        starting_equity=equity, slippage_bps=0.0, commission_per_trade=0.0,
                        journal_dir=tmp_path)
    router = SessionRouter(Router.build_default(mode="paper", broker=paper, state_dir=tmp_path),
                           paper, account_number="PAPER-001", clock=lambda: now[0])
    paper.place_order(Order(symbol="/COIL", symbolId=1, action=OrderAction.BUY,
                            orderType=OrderType.MARKET, totalQuantity=1))
    roller = make_roller(book, paper, router, account_number="PAPER-001", is_tradeable=lambda s: True)
    now[0] = datetime(2026, 9, 24, 12, tzinfo=UTC)
    return book, paper, roller


def test_roll_exits_old_contract_and_reenters_new_without_booking_the_spread(tmp_path: Path) -> None:
    book, paper, roller = _roll_setup(tmp_path, equity=1_000_000.0)
    equity_before = paper.equity("PAPER-001")
    roller(equity=equity_before, existing_risk=0.0, open_positions=1)

    assert book.current["/COIL"].local_symbol == "COILZ6"
    (pos,) = paper.positions("PAPER-001")
    assert pos.openQuantity == 1
    assert pos.averageEntryPrice == pytest.approx(71.01 * 1000)     # re-entered on the new contract
    assert paper.equity("PAPER-001") == pytest.approx(equity_before)  # calendar spread not booked as P&L


def test_roll_reentry_refused_by_the_position_cap_leaves_the_book_flat(tmp_path: Path) -> None:
    book, paper, roller = _roll_setup(tmp_path, equity=100_000.0)
    roller(equity=paper.equity("PAPER-001"), existing_risk=0.0, open_positions=1)
    assert book.current["/COIL"].local_symbol == "COILZ6"
    assert paper.positions("PAPER-001") == []                        # gate wins: flat, not stuck


def test_roll_waits_while_the_venue_is_closed_and_flat_symbols_just_switch(tmp_path: Path) -> None:
    now = [datetime(2026, 9, 24, 12, tzinfo=UTC)]
    book = FuturesBook(roll_bdays=5, clock=lambda: datetime(2026, 9, 14, tzinfo=UTC))
    book.add(spec_from_ib_details(BRENT))
    book.clock = lambda: now[0]
    raw = _Feed(book)
    paper = PaperBroker(feed=CurrencyNormalizingBroker(raw, FxRates(lambda p: None, "USD"),
                                                       multiplier_for=book.multiplier_for),
                        slippage_bps=0.0, journal_dir=tmp_path)
    roller = make_roller(book, paper, SimpleNamespace(submit=lambda *a, **k: None),
                         account_number="PAPER-001", is_tradeable=lambda s: False)
    roller(equity=1.0, existing_risk=0.0, open_positions=0)
    assert book.current["/COIL"].local_symbol == "COILZ6"             # flat: switched immediately

    book.current["/COIL"] = book.specs["/COIL"].contracts[0]
    paper.place_order(Order(symbol="/COIL", symbolId=1, action=OrderAction.BUY,
                            orderType=OrderType.MARKET, totalQuantity=1))
    roller(equity=1.0, existing_risk=0.0, open_positions=1)
    assert book.current["/COIL"].local_symbol == "COILX6"             # held + closed: waits


def test_missing_active_contract_reads_as_unresolvable() -> None:
    from trading_live_claude.brokers.base import BrokerError, OrderRejected
    from trading_live_claude.brokers.ib import IBBroker

    ib = IBBroker(enable_live_orders=True)
    ib.futures_contract_for = FuturesBook().contract_for
    with pytest.raises(BrokerError, match="no active futures contract"):
        ib._futures_resolution("/COIL")
    with pytest.raises(OrderRejected, match="live futures orders are not implemented"):
        ib.place_order(Order(symbol="/COIL", symbolId=1, action=OrderAction.BUY,
                             orderType=OrderType.MARKET, totalQuantity=1))


def test_stale_quote_is_still_a_stale_quote() -> None:
    assert issubclass(StaleQuote, Exception)
