from __future__ import annotations

import zlib

from trading_live_claude.microstructure.kraken_l2 import (
    KrakenOrderBook,
    _checksum_token,
    parse_book_message,
    subscribe_message,
)

# The worked example from Kraken's checksum guide: this concatenated top-10 string must CRC32 to
# 3310070434. Validates our CRC32 step and byte handling independent of book reconstruction.
# https://docs.kraken.com/api/docs/guides/spot-ws-book-v2/
_KRAKEN_EXAMPLE = (
    "45285210000045286415457195345286615457110945289615456091145290215890660452918154553491"
    "452947445474945296135380000452975994554245299518772827452835100000004528341545820154528"
    "211000000045281010000000452803154592586452790799000045277633101034527753000000045277315"
    "460273745276615445238"
)


def test_checksum_token_formatting() -> None:
    assert _checksum_token("45285.2") == "452852"       # decimal point removed
    assert _checksum_token("0.00010000") == "10000"     # leading zeros stripped, trailing kept
    assert _checksum_token("000100000") == "100000"
    assert _checksum_token("0") == "0"


def test_crc32_matches_kraken_documented_vector() -> None:
    assert zlib.crc32(_KRAKEN_EXAMPLE.encode("ascii")) & 0xFFFFFFFF == 3310070434


def test_checksum_builds_asks_then_bids_in_order() -> None:
    book = KrakenOrderBook()
    book.apply_snapshot(bids=[{"price": "9.0", "qty": "2.0"}], asks=[{"price": "10.0", "qty": "1.0"}])
    # asks (low->high) first: 100 + 10, then bids (high->low): 90 + 20
    expected = zlib.crc32(b"100109020") & 0xFFFFFFFF
    assert book.checksum() == expected


def test_snapshot_builds_book_with_microprice_and_imbalance() -> None:
    book = KrakenOrderBook()
    book.apply_snapshot(bids=[{"price": "9", "qty": "2"}], asks=[{"price": "10", "qty": "1"}])
    lob = book.to_limit_order_book()
    assert lob.best_bid.price == 9.0 and lob.best_ask.price == 10.0
    assert lob.mid == 9.5
    assert lob.microprice > lob.mid          # bid is heavier -> microprice leans up
    assert abs(lob.imbalance() - (1.0 / 3.0)) < 1e-9


def test_update_removes_level_on_zero_qty_and_truncates() -> None:
    book = KrakenOrderBook(depth=2)
    book.apply_snapshot(
        bids=[{"price": "9", "qty": "1"}, {"price": "8", "qty": "1"}, {"price": "7", "qty": "1"}],
        asks=[{"price": "10", "qty": "1"}, {"price": "11", "qty": "1"}],
    )
    assert book.to_limit_order_book().best_bid.price == 9.0
    assert len(book._bids) == 2  # truncated to depth
    book.apply_update(bids=[{"price": "9", "qty": "0"}], asks=[])  # remove the top bid
    assert book.to_limit_order_book().best_bid.price == 8.0
    book.apply_update(bids=[{"price": "8", "qty": "5"}], asks=[])  # resize a level
    assert book.to_limit_order_book().best_bid.size == 5.0


def test_parse_book_message_filters_non_book() -> None:
    snap = {"channel": "book", "type": "snapshot",
            "data": [{"symbol": "BTC/USD", "bids": [{"price": "9", "qty": "2"}],
                      "asks": [{"price": "10", "qty": "1"}], "checksum": 123}]}
    parsed = parse_book_message(snap)
    assert parsed is not None
    mtype, sym, _bids, _asks, checksum = parsed
    assert mtype == "snapshot" and sym == "BTC/USD" and checksum == 123
    assert parse_book_message({"channel": "status", "type": "update", "data": [{}]}) is None
    assert parse_book_message({"channel": "heartbeat"}) is None


def test_subscribe_message_shape() -> None:
    import json
    msg = json.loads(subscribe_message("BTC/USD", 10))
    assert msg["method"] == "subscribe"
    assert msg["params"]["channel"] == "book"
    assert msg["params"]["symbol"] == ["BTC/USD"]
    assert msg["params"]["depth"] == 10
