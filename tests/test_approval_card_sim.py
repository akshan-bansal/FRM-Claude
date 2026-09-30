"""WYSIWYS checks in the laptop card simulator (mirrors the firmware logic)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from trading_live_claude.brokers.models import OrderAction
from trading_live_claude.execution.approval import CardRegistry, InMemoryApprovalStore
from trading_live_claude.execution.router import OrderIntent

_SIM_PATH = Path(__file__).resolve().parent.parent / "scripts" / "approval_card_sim.py"
_spec = importlib.util.spec_from_file_location("approval_card_sim", _SIM_PATH)
assert _spec and _spec.loader
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)


@pytest.fixture
def prompt() -> dict:
    store = InMemoryApprovalStore(CardRegistry())
    intent = OrderIntent(
        symbol="XIC.TO", action=OrderAction.BUY, shares=10, entry=31.05,
        stop=30.5, target=32.0, strategy="test", risk_dollars=5.5,
        account_number="PAPER-001", symbolId=1,
    )
    return store.publish(intent, mode="paper", broker="ib", ttl_seconds=30).to_dict()


def test_genuine_prompt_parses_to_the_signed_fields(prompt: dict) -> None:
    signed = sim.parse_canonical(prompt)
    assert signed is not None
    assert signed["broker"] == "ib"
    assert signed["symbol"] == "XIC.TO"
    assert int(signed["shares"]) == 10
    assert signed["intent_id"] == prompt["intent_id"]
    assert signed["nonce"] == prompt["nonce"]


def test_display_comes_from_signed_bytes_not_json_fields(prompt: dict) -> None:
    signed = sim.parse_canonical(prompt)
    assert signed is not None
    spoofed = {**prompt, "symbol": "TSLA", "shares": 1, "notional_usd": 1.0, "broker": "kraken"}
    line = sim.render_prompt(spoofed, signed)
    assert "XIC.TO" in line and "[IB]" in line and "10sh" in line
    assert "TSLA" not in line and "KRAKEN" not in line


def test_fractional_crypto_quantity_is_displayed_as_signed() -> None:
    store = InMemoryApprovalStore(CardRegistry())
    intent = OrderIntent(
        symbol="BTC/USD", action=OrderAction.BUY, shares=0.05, entry=42000.0,  # type: ignore[arg-type]
        stop=41000.0, target=44000.0, strategy="macd", risk_dollars=50.0,
        account_number="K1", symbolId=1,
    )
    p = store.publish(intent, mode="paper", broker="kraken", ttl_seconds=30).to_dict()
    signed = sim.parse_canonical(p)
    assert signed is not None and signed["shares"] == "0.05"
    assert "0.05sh" in sim.render_prompt(p, signed)


def test_canonical_bound_to_another_intent_is_refused(prompt: dict) -> None:
    assert sim.parse_canonical({**prompt, "intent_id": "some-other-intent"}) is None


@pytest.mark.parametrize("canonical", [
    "",
    "ib|Buy|XIC.TO|10",
    "ib|Buy|XIC.TO|10|31.0500|310.50|PAPER-001|iid|nonce|extra",
    "ib|Buy||10|31.0500|310.50|PAPER-001|iid|nonce",
])
def test_malformed_canonical_is_refused(prompt: dict, canonical: str) -> None:
    assert sim.parse_canonical({**prompt, "canonical": canonical, "intent_id": "iid"}) is None


# --- error-body decoding (2026-09-17) --------------------------------------
# The HTTP helpers did json.loads(e.read()) on the error path, so a shim 500 — which uvicorn
# returns as text/plain "Internal Server Error" — raised JSONDecodeError and killed the whole card
# process, hiding the status code and making a server bug look like a client crash.

def test_decode_non_json_body_does_not_raise() -> None:
    assert sim._decode(b"Internal Server Error") == {"error": "Internal Server Error"}


def test_decode_empty_body() -> None:
    assert sim._decode(b"") == {}
    assert sim._decode(b"   \n") == {}


def test_decode_json_object_passes_through() -> None:
    assert sim._decode(b'{"detail": "nope"}') == {"detail": "nope"}


def test_decode_json_non_object_is_wrapped() -> None:
    assert sim._decode(b"[1, 2]") == {"data": [1, 2]}


def test_decode_truncates_a_huge_html_error_page() -> None:
    body = ("<html>" + "x" * 5000).encode()
    out = sim._decode(body)
    assert len(out["error"]) == 500


def test_decode_tolerates_invalid_utf8() -> None:
    assert "error" in sim._decode(b"\xff\xfe broken")


def test_helpers_return_status_zero_when_shim_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused connection must be a reportable status, not an exception out of _get/_post."""
    import urllib.error

    def _boom(*_a, **_k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(sim.urllib.request, "urlopen", _boom)
    status, body = sim._get("http://127.0.0.1:1/v1/intents/pending")
    assert status == 0 and "refused" in body["error"]
    status, body = sim._post("http://127.0.0.1:1/v1/card/register", {"a": 1})
    assert status == 0 and "refused" in body["error"]


def test_error_status_surfaces_instead_of_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 500 with a text/plain body comes back as (500, {"error": ...})."""
    import io
    import urllib.error

    def _http_error(*_a, **_k):
        raise urllib.error.HTTPError(
            "http://x/v1/intents/pending", 500, "Internal Server Error",
            {}, io.BytesIO(b"Internal Server Error"),
        )

    monkeypatch.setattr(sim.urllib.request, "urlopen", _http_error)
    status, body = sim._get("http://x/v1/intents/pending")
    assert status == 500
    assert body == {"error": "Internal Server Error"}


def test_transport_error_covers_a_raw_connection_reset() -> None:
    """A reset mid-read is an OSError but NOT a URLError — it used to escape and kill the card."""
    label = sim._transport_error(ConnectionResetError(10054, "forcibly closed"))
    assert label.startswith("ConnectionResetError")


def test_get_survives_a_connection_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    def _reset(*_a, **_k):
        raise ConnectionResetError(10054, "forcibly closed by the remote host")

    monkeypatch.setattr(sim.urllib.request, "urlopen", _reset)
    status, body = sim._get("http://127.0.0.1:8787/v1/intents/pending")
    assert status == 0
    assert "ConnectionResetError" in body["error"]


def test_registration_does_not_claim_success_on_a_dead_shim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Status 0 is a transport failure, not a server yes; the card must exit, not start polling."""
    monkeypatch.setattr(sim, "_post", lambda *_a, **_k: (0, {"error": "URLError: refused"}))
    monkeypatch.setattr(sim, "REGISTER_TIMEOUT_SECONDS", 0.0)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    with pytest.raises(SystemExit):
        sim.run(shim_url="http://127.0.0.1:1", card_id="c1",
                key=Ed25519PrivateKey.generate(), auto="accept", poll_interval=0.01)
