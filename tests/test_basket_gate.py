"""Card-signed basket gate: entries outside the signed basket are rejected, exits never are."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.test_router import _intent, _StubBroker
from trading_live_claude.brokers.models import OrderAction
from trading_live_claude.execution.basket import (
    BasketBook,
    BasketGate,
    append_row,
    new_row,
    normalize,
)
from trading_live_claude.execution.router import Router

KEY = Ed25519PrivateKey.generate()
OTHER = Ed25519PrivateKey.generate()


def _verify(card_id: str, canon: bytes, sig: bytes) -> bool:
    if card_id != "card-a":
        return False
    try:
        KEY.public_key().verify(sig, canon)
        return True
    except Exception:
        return False


def _signed(venue: str, symbols: list[str], key=KEY, card_id: str = "card-a") -> dict:
    return new_row(venue=venue, symbols=symbols, analysis={"n": len(symbols)}, card_id=card_id,
                   sign=key.sign)


def _router(tmp_path: Path, venue: str = "qt") -> tuple[Router, Path]:
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path)
    path = tmp_path / "baskets.jsonl"
    router.basket_gate = BasketGate(venue, BasketBook(path, _verify))
    return router, path


def _submit(router: Router, intent=None):
    return router.submit(intent or _intent(), equity=100_000, existing_risk=0, open_positions=0)


def test_not_enforced_when_no_gate_is_set(tmp_path: Path) -> None:
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path)
    assert router.basket_gate is None
    assert _submit(router) is not None            # sessions that predate baskets keep trading


def test_venue_with_no_basket_takes_no_entries(tmp_path: Path) -> None:
    router, _ = _router(tmp_path)
    assert _submit(router) is None
    assert "no card-signed basket" in (tmp_path / "rejected.jsonl").read_text(encoding="utf-8")


def test_entry_inside_basket_passes_and_outside_is_rejected(tmp_path: Path) -> None:
    router, path = _router(tmp_path)
    append_row(_signed("qt", ["AAPL", "msft"]), path)
    assert _submit(router) is not None
    other = _intent()
    other.symbol = "TSLA"
    assert _submit(router, other) is None
    assert "outside the card-signed qt basket" in (tmp_path / "rejected.jsonl").read_text(encoding="utf-8")


def test_a_basket_for_another_venue_does_not_admit(tmp_path: Path) -> None:
    router, path = _router(tmp_path, venue="qt")
    append_row(_signed("kraken", ["AAPL"]), path)
    assert _submit(router) is None


def test_exits_are_never_blocked(tmp_path: Path) -> None:
    router, path = _router(tmp_path)
    append_row(_signed("qt", ["MSFT"]), path)      # AAPL is not in the basket
    sell = _intent()
    sell.action = OrderAction.SELL
    sell.stop = 104.0                              # a valid stop for a sell
    dec = router._gate(sell, equity=100_000, existing_risk=0, open_positions=1)
    assert not any("basket" in r for r in dec.rejected_reasons)


def test_signature_from_an_unregistered_key_does_not_count(tmp_path: Path) -> None:
    router, path = _router(tmp_path)
    append_row(_signed("qt", ["AAPL"], key=OTHER), path)
    assert _submit(router) is None


def test_unknown_card_id_does_not_count(tmp_path: Path) -> None:
    router, path = _router(tmp_path)
    append_row(_signed("qt", ["AAPL"], card_id="card-z"), path)
    assert _submit(router) is None


def test_hand_edited_symbols_do_not_count(tmp_path: Path) -> None:
    router, path = _router(tmp_path)
    row = _signed("qt", ["MSFT"])
    row["symbols"] = ["AAPL"]                       # display says AAPL, signature covers MSFT
    append_row(row, path)
    assert _submit(router) is None
    assert router.basket_gate.book.rejected_rows == 1


def test_later_signed_basket_supersedes_and_an_empty_one_withdraws(tmp_path: Path) -> None:
    router, path = _router(tmp_path)
    append_row(_signed("qt", ["AAPL"]), path)
    assert _submit(router) is not None
    append_row(_signed("qt", []), path)             # signed withdrawal: nothing is allowed
    assert _submit(router) is None


def test_garbage_lines_are_ignored_not_fatal(tmp_path: Path) -> None:
    router, path = _router(tmp_path)
    path.write_text("not json\n" + json.dumps({"venue": "qt"}) + "\n", encoding="utf-8")
    append_row(_signed("qt", ["AAPL"]), path)
    assert _submit(router) is not None
    assert router.basket_gate.book.rejected_rows == 2


def test_normalize_sorts_dedupes_and_uppercases() -> None:
    assert normalize(["msft", "AAPL", "aapl", " "]) == ("AAPL", "MSFT")


@pytest.mark.parametrize("venue", ["QT", "qt", "Qt"])
def test_venue_match_is_case_insensitive(tmp_path: Path, venue: str) -> None:
    router, path = _router(tmp_path, venue=venue)
    append_row(_signed("qt", ["AAPL"]), path)
    assert _submit(router) is not None


def test_registry_verifier_reads_cards_paired_after_the_process_started(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization

    from trading_live_claude.execution.approval_sqlite import SqliteCardRegistry
    from trading_live_claude.execution.basket import attach_basket_gate

    db = tmp_path / "approval.db"
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path)
    attach_basket_gate(router, "qt", tmp_path, enabled=True)
    append_row(_signed("qt", ["AAPL"], card_id="card-real"), tmp_path / "baskets.jsonl")
    assert _submit(router) is None                              # card not paired yet
    pem = KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    with SqliteCardRegistry(db) as reg:
        reg.register("card-real", pem)                          # paired later, by another process
    (tmp_path / "baskets.jsonl").touch()                        # book re-reads on file change
    assert _submit(router) is not None


def test_attach_is_a_no_op_when_disabled(tmp_path: Path) -> None:
    from trading_live_claude.execution.basket import attach_basket_gate
    router = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path)
    assert attach_basket_gate(router, "qt", tmp_path, enabled=False) is None
    assert router.basket_gate is None


def test_a_future_matches_with_or_without_the_leading_slash(tmp_path: Path) -> None:
    router, path = _router(tmp_path)
    append_row(_signed("qt", ["/AAPL"]), path)                  # signed with the slash
    assert _submit(router) is not None                          # intent symbol is AAPL


def test_ib_basket_accepts_futures_roots_and_refuses_equities() -> None:
    from trading_live_claude.execution.basket import ib_refusal
    assert ib_refusal("GC") is None and ib_refusal("/SIL") is None
    assert "futures only" in (ib_refusal("AAPL") or "")
