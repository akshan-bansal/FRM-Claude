"""HTTP shim between the ApprovalRouter and an ESP32-hosted approval card.

Stdlib only. Runs on the user's laptop (or a small always-on box) at
``127.0.0.1:8787`` by default. Two audiences:

  * the trading engine (strategy / daemon)     -> POST /intents,
                                                 POST /intents/{id}/response
                                                 (only used when the shim itself
                                                 owns the store; see below)
  * the ESP32 card (over BLE bridge or direct) -> GET /intents/pending,
                                                  POST /card/register,
                                                  POST /intents/{id}/response

In production the trading engine imports :class:`ApprovalRouter` directly and
holds its own reference to :class:`InMemoryApprovalStore`. The shim's job is
to expose that same store to the card over the network. To keep this file
runnable standalone we let it own a store and give the engine a REST path
(``POST /intents``) to publish through; if you'd rather share a store
in-process, import ``run_shim(store, registry)`` and pass your own.

Wire security: this shim binds to loopback and is intended to sit behind a
BLE-tethered phone (which acts as an HTTP proxy) or a Cloudflare / Tailscale
tunnel. It does NOT implement TLS. Do not expose it to the public internet
directly.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trading_live_claude.brokers.models import OrderAction  # noqa: E402
from trading_live_claude.execution.approval import (  # noqa: E402
    CardRegistry,
    InMemoryApprovalStore,
)
from trading_live_claude.execution.router import OrderIntent  # noqa: E402
from trading_live_claude.intel.vs_engine import DEFAULT_WRITEUP_DIR  # noqa: E402


# --------------------------------------------------------------------------- #
# handler                                                                     #
# --------------------------------------------------------------------------- #

def make_handler(
    store: InMemoryApprovalStore,
    registry: CardRegistry,
    *,
    writeup_dir: Path = DEFAULT_WRITEUP_DIR,
):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TradeCardShim/0.1"

        # -- helpers ----------------------------------------------------- #

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"bad json: {e}"})
                raise

        def _send_json(self, status: int, body: dict | list) -> None:
            payload = json.dumps(body, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write(
                f"[{datetime.now(UTC).isoformat()}] {self.address_string()} {fmt % args}\n"
            )

        # -- routing ----------------------------------------------------- #

        def do_GET(self) -> None:
            if self.path.startswith("/intents/pending"):
                prompts = [p.to_dict() for p in store.pending()]
                self._send_json(HTTPStatus.OK, {"prompts": prompts})
                return
            if self.path.startswith("/intents/"):
                intent_id = self.path.split("/")[2].split("?")[0]
                for p in store.pending():
                    if p.intent_id == intent_id:
                        self._send_json(HTTPStatus.OK, p.to_dict())
                        return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown or resolved intent"})
                return
            if self.path == "/healthz":
                self._send_json(HTTPStatus.OK, {"ok": True, "pending": len(store.pending())})
                return
            if self.path.startswith("/intel/"):
                # /intel/{ref} — VS-engine writeup lookup for the card's CENTER-button
                # detail view. Path-traversal defense: only accept the ref segment as
                # a filename, refuse anything that would escape the writeup dir.
                ref = self.path.split("/", 2)[2].split("?")[0]
                if not ref or "/" in ref or "\\" in ref or ".." in ref:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad intel_ref"})
                    return
                path = writeup_dir / f"{ref}.json"
                try:
                    resolved = path.resolve()
                    resolved.relative_to(writeup_dir.resolve())
                except (OSError, ValueError):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad intel_ref"})
                    return
                if not resolved.exists():
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "no writeup"})
                    return
                try:
                    payload = json.loads(resolved.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as e:
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                                    {"error": f"unreadable writeup: {e}"})
                    return
                self._send_json(HTTPStatus.OK, payload)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "no route"})

        def do_POST(self) -> None:
            try:
                body = self._read_json()
            except json.JSONDecodeError:
                return  # response already sent

            if self.path == "/card/register":
                try:
                    card_id = body["card_id"]
                    pubkey_pem = body["pubkey_pem"].encode("utf-8")
                    registry.register(card_id, pubkey_pem)
                    self._send_json(HTTPStatus.CREATED, {"card_id": card_id})
                except (KeyError, ValueError) as e:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
                return

            if self.path == "/intents":
                try:
                    intent = OrderIntent(
                        symbol=body["symbol"],
                        action=OrderAction(body["action"]),
                        shares=int(body["shares"]),
                        entry=float(body["entry"]),
                        stop=float(body["stop"]),
                        target=(float(body["target"]) if body.get("target") is not None else None),
                        strategy=body["strategy"],
                        risk_dollars=float(body["risk_dollars"]),
                        account_number=body["account_number"],
                        symbolId=body.get("symbolId"),
                    )
                    ttl = float(body.get("ttl_seconds", 90.0))
                    mode = body.get("mode", "paper")
                    broker = body.get("broker", "questrade")
                    if broker not in {"ib", "kraken", "questrade"}:
                        self._send_json(HTTPStatus.BAD_REQUEST,
                                        {"error": f"unsupported broker: {broker}"})
                        return
                    thesis = body.get("thesis", "")
                    intel_ref = body.get("intel_ref", "")
                    prompt = store.publish(
                        intent, mode=mode, broker=broker, ttl_seconds=ttl,
                        thesis=thesis, intel_ref=intel_ref,
                    )
                    self._send_json(HTTPStatus.CREATED, prompt.to_dict())
                except (KeyError, ValueError) as e:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
                return

            if self.path.startswith("/intents/") and self.path.endswith("/response"):
                intent_id = self.path.split("/")[2]
                try:
                    decision = body["decision"].upper()
                    card_id = body["card_id"]
                    sig_b64 = body["signature"]
                    signature = base64.b64decode(sig_b64)
                except (KeyError, ValueError) as e:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
                    return
                if decision not in {"ACCEPT", "DECLINE"}:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "decision must be ACCEPT or DECLINE"})
                    return
                ok = store.respond(intent_id, decision=decision, card_id=card_id, signature=signature)
                self._send_json(
                    HTTPStatus.OK if ok else HTTPStatus.UNAUTHORIZED,
                    {"accepted": ok, "intent_id": intent_id},
                )
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "no route"})

    return Handler


# --------------------------------------------------------------------------- #
# entrypoint                                                                  #
# --------------------------------------------------------------------------- #

def run_shim(
    store: InMemoryApprovalStore,
    registry: CardRegistry,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    writeup_dir: Path = DEFAULT_WRITEUP_DIR,
) -> None:
    handler = make_handler(store, registry, writeup_dir=writeup_dir)
    httpd = ThreadingHTTPServer((host, port), handler)
    sys.stderr.write(f"approval shim listening on http://{host}:{port}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("shim shutting down\n")
        httpd.shutdown()


def start_shim_thread(
    store: InMemoryApprovalStore,
    registry: CardRegistry,
    host: str,
    port: int,
    writeup_dir: Path | None = None,
):
    """Daemon-thread wrapper for :func:`run_shim`. Injected into
    ``execution.approval.wire_card_approval`` by the paper scripts.

    ``writeup_dir`` defaults to the current value of the module attribute
    (re-read at call time so tests can monkeypatch it)."""
    import threading

    resolved = writeup_dir if writeup_dir is not None else DEFAULT_WRITEUP_DIR

    def _serve() -> None:
        try:
            run_shim(store, registry, host=host, port=port, writeup_dir=resolved)
        except Exception as e:  # pragma: no cover
            sys.stderr.write(f"[approval-shim] died: {e}\n")

    t = threading.Thread(target=_serve, name="approval-shim", daemon=True)
    t.start()
    return t


def main() -> None:
    ap = argparse.ArgumentParser(description="TradeCard approval shim")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    registry = CardRegistry()
    store = InMemoryApprovalStore(registry)
    run_shim(store, registry, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
