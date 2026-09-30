"""Fake ESP32 approval card — long-polls the shim, signs prompts, replies.

Two modes:

  ``--auto``        auto-ACCEPT (or --auto=decline) every prompt. Great for
                    end-to-end smoke tests of the approval loop without any
                    hardware.
  ``--interactive`` (default) show each prompt and wait for a keypress
                    (a=ACCEPT, d=DECLINE, s=skip / let it expire). Mimics the
                    tap-to-approve UX the real card will have.

The keypair is persisted to ``state/card_sim/{card_id}.pem`` (private) and
registered with the shim on first run. Re-runs reuse it.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# --------------------------------------------------------------------------- #
# key management                                                              #
# --------------------------------------------------------------------------- #

def load_or_create_key(key_path: Path) -> Ed25519PrivateKey:
    if key_path.exists():
        return serialization.load_pem_private_key(  # type: ignore[return-value]
            key_path.read_bytes(), password=None
        )
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return key


def pubkey_pem(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


# --------------------------------------------------------------------------- #
# HTTP helpers                                                                #
# --------------------------------------------------------------------------- #

_AUTH_HEADERS: dict[str, str] = {}


def _decode(raw: bytes) -> dict:
    """Body as a dict. A non-JSON body becomes {"error": "<text>"} instead of raising.

    2026-09-17: these helpers did ``json.loads(e.read())`` on the error path, so a shim 500 —
    which uvicorn returns as ``text/plain`` "Internal Server Error" — raised JSONDecodeError and
    killed the whole card process. That hid the real status code and made a server bug look like a
    client crash. Never let an error body's content type decide whether the card survives.
    """
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"error": text[:500]}
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def _transport_error(e: OSError) -> str:
    """Label for any transport failure, without letting it escape.

    ``OSError``, not ``URLError``: when the shim shuts down mid-response urllib surfaces a raw
    ``ConnectionResetError`` (WinError 10054) from the socket read, which is an OSError but NOT a
    URLError — it killed the card with a traceback at the end of the 2026-09-17 end-to-end run.
    A real card sitting on a desk must ride out the server restarting, so every transport fault
    becomes status 0 and the poll loop simply tries again.
    """
    reason = getattr(e, "reason", None) or e
    return f"{type(e).__name__}: {reason}"


def _post(url: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **_AUTH_HEADERS},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, _decode(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _decode(e.read())
    except OSError as e:                    # shim down / refused / reset mid-read — keep polling
        return 0, {"error": _transport_error(e)}


def _get(url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers=_AUTH_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, _decode(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _decode(e.read())
    except OSError as e:
        return 0, {"error": _transport_error(e)}


# --------------------------------------------------------------------------- #
# UI helpers                                                                  #
# --------------------------------------------------------------------------- #

REGISTER_TIMEOUT_SECONDS = 30.0   # a card may boot before the shim does

CANONICAL_FIELDS = (
    "broker", "action", "symbol", "shares", "entry",
    "notional_usd", "account", "intent_id", "nonce",
)


def _fingerprint(canonical: str, chars: int = 8) -> str:
    """``7F3A...91C2`` over the canonical bytes — computed HERE, not taken from the server.

    Kept as a local implementation (like ``parse_canonical``) rather than importing the package
    helper: a card verifies for itself, and real firmware has no access to our Python. Mirrors what
    ``firmware/tradecard/main/main.c`` must display.
    """
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest().upper()
    half = chars // 2
    return f"{digest[:half]}...{digest[-half:]}"


def parse_canonical(p: dict) -> dict[str, str] | None:
    """Fields of the exact bytes the card will sign, or None if they can't be trusted.

    Order details are displayed from these, never from the prompt's separate JSON
    fields — otherwise a shim could show one trade and have the card sign another.
    Mirrors ``parse_canonical`` in ``firmware/tradecard/main/main.c``.
    """
    parts = str(p.get("canonical", "")).split("|")
    if len(parts) != len(CANONICAL_FIELDS) or not all(parts):
        return None
    signed = dict(zip(CANONICAL_FIELDS, parts, strict=True))
    if signed["intent_id"] != p.get("intent_id"):
        return None
    return signed


def render_prompt(p: dict, signed: dict[str, str]) -> str:
    """The compact line the real card will show. Order fields come from ``signed``."""
    seconds_left = max(
        0,
        int((datetime.fromisoformat(p["expires_at"]) - datetime.now(UTC)).total_seconds()),
    )
    header = (
        f"[{signed['broker'].upper()}] {signed['action']:>4} {signed['symbol']:<8} "
        f"{signed['shares']:>4}sh  ${float(signed['notional_usd']):>10,.2f}  "
        f"R=${p['risk_dollars']:>7,.2f}  [{p['strategy']}]  {seconds_left}s"
    )
    thesis = p.get("thesis") or ""
    if thesis:
        return header + f"\n  intel> {thesis}"
    return header


# --------------------------------------------------------------------------- #
# main loop                                                                   #
# --------------------------------------------------------------------------- #

def run(
    *,
    shim_url: str,
    card_id: str,
    key: Ed25519PrivateKey,
    auto: str | None,
    poll_interval: float,
) -> None:
    # Register (idempotent on the server side; re-registering is fine). Status 0 means the
    # transport failed, not that the server said yes — a card powered on before the shim must
    # wait rather than fall through to polling as if it were paired.
    body = {"card_id": card_id, "pubkey_pem": pubkey_pem(key).decode("utf-8")}
    deadline = time.monotonic() + REGISTER_TIMEOUT_SECONDS
    while True:
        status, resp = _post(f"{shim_url}/v1/card/register", body)
        if status == 0 and time.monotonic() < deadline:
            print(f"waiting for shim at {shim_url}: {resp.get('error')}", file=sys.stderr)
            time.sleep(min(poll_interval, 2.0))
            continue
        break
    if status == 0 or status >= 400:
        print(f"registration failed: {status} {resp}", file=sys.stderr)
        sys.exit(1)
    print(f"registered card_id={card_id} with {shim_url}")

    seen: set[str] = set()
    while True:
        try:
            status, resp = _get(f"{shim_url}/v1/intents/pending")
        except urllib.error.URLError as e:
            print(f"shim unreachable: {e}", file=sys.stderr)
            time.sleep(poll_interval)
            continue
        if status != 200:
            print(f"pending fetch failed: {status} {resp}", file=sys.stderr)
            time.sleep(poll_interval)
            continue

        for prompt in resp.get("prompts", []):
            if prompt["intent_id"] in seen:
                continue
            seen.add(prompt["intent_id"])
            signed = parse_canonical(prompt)
            if signed is None:
                print(f"\nREFUSED {prompt['intent_id']}: canonical malformed or bound "
                      "to another intent — not signing", file=sys.stderr)
                continue
            # Continuity check (phase 6). The card recomputes the fingerprint from the bytes it is
            # about to sign and compares it against the one the server sent for display. A mismatch
            # means the dashboard and the device would show different things for a single
            # signature — exactly the drift the fingerprint exists to catch — so refuse to sign.
            local_fp = _fingerprint(prompt["canonical"])
            served_fp = str(prompt.get("fingerprint", ""))
            if served_fp and served_fp != local_fp:
                print(f"\nREFUSED {prompt['intent_id']}: fingerprint mismatch — served "
                      f"{served_fp}, computed {local_fp} over the bytes to be signed",
                      file=sys.stderr)
                continue
            print("\n" + "=" * 78)
            print(render_prompt(prompt, signed))
            print(f"  HASH {local_fp}   (recomputed here, over the exact bytes to be signed)")
            print("=" * 78)

            decision = _decide(prompt, auto=auto)
            if decision is None:
                print("(skipped — will expire)")
                continue

            sig = key.sign(prompt["canonical"].encode("utf-8"))
            body = {
                "decision": decision,
                "card_id": card_id,
                "signature": base64.b64encode(sig).decode("ascii"),
            }
            status, resp = _post(
                f"{shim_url}/v1/intents/{prompt['intent_id']}/response", body
            )
            print(f"-> {decision}: {status} {resp}")

        time.sleep(poll_interval)


def _decide(prompt: dict, *, auto: str | None) -> str | None:
    if auto == "accept":
        return "ACCEPT"
    if auto == "decline":
        return "DECLINE"
    # interactive
    while True:
        try:
            choice = input("[a]ccept / [d]ecline / [s]kip > ").strip().lower()
        except EOFError:
            return None
        if choice in {"a", "accept"}:
            return "ACCEPT"
        if choice in {"d", "decline"}:
            return "DECLINE"
        if choice in {"s", "skip", ""}:
            return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Fake ESP32 approval card")
    ap.add_argument("--shim", default="http://127.0.0.1:8787")
    ap.add_argument("--card-id", default="card-sim-001")
    ap.add_argument("--key-file", type=Path, default=Path("state/card_sim/card-sim-001.pem"))
    ap.add_argument(
        "--auto",
        choices=["accept", "decline"],
        default=None,
        help="auto-respond to every prompt; omit for interactive tap-simulation",
    )
    ap.add_argument("--poll", type=float, default=1.0, help="poll interval seconds")
    ap.add_argument("--auth-token", default=None,
                    help="Bearer token to send with every request; must match the "
                         "shim's --auth-token (paper scripts print this at startup).")
    args = ap.parse_args()
    if args.auth_token:
        _AUTH_HEADERS["Authorization"] = f"Bearer {args.auth_token}"

    key = load_or_create_key(args.key_file)
    print(f"card key: {args.key_file}")
    run(
        shim_url=args.shim.rstrip("/"),
        card_id=args.card_id,
        key=key,
        auto=args.auto,
        poll_interval=args.poll,
    )


if __name__ == "__main__":
    main()
