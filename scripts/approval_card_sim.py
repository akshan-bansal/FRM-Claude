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

def _post(url: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


# --------------------------------------------------------------------------- #
# UI helpers                                                                  #
# --------------------------------------------------------------------------- #

def render_prompt(p: dict) -> str:
    """The compact line the real card e-ink will show."""
    seconds_left = max(
        0,
        int((datetime.fromisoformat(p["expires_at"]) - datetime.now(UTC)).total_seconds()),
    )
    broker = (p.get("broker") or "?").upper()
    header = (
        f"[{broker}] {p['action']:>4} {p['symbol']:<8} {p['shares']:>4}sh  "
        f"${p['notional_usd']:>10,.2f}  R=${p['risk_dollars']:>7,.2f}  "
        f"[{p['strategy']}]  {seconds_left}s"
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
    # Register (idempotent on the server side; re-registering is fine).
    status, resp = _post(
        f"{shim_url}/card/register",
        {"card_id": card_id, "pubkey_pem": pubkey_pem(key).decode("utf-8")},
    )
    if status >= 400:
        print(f"registration failed: {status} {resp}", file=sys.stderr)
        sys.exit(1)
    print(f"registered card_id={card_id} with {shim_url}")

    seen: set[str] = set()
    while True:
        try:
            status, resp = _get(f"{shim_url}/intents/pending")
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
            print("\n" + "=" * 78)
            print(render_prompt(prompt))
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
                f"{shim_url}/intents/{prompt['intent_id']}/response", body
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
    args = ap.parse_args()

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
