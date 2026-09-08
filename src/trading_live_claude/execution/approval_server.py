"""HTTP shim between the ApprovalRouter and an ESP32-hosted approval card.

Stdlib-only. Runs on the user's laptop (or a small always-on box) at
``127.0.0.1:8787`` by default. This module holds the transport surface so
the rest of ``execution/`` stays free of HTTP concerns; ``scripts/approval_shim.py``
is a thin CLI wrapper around :func:`run_shim`.

Two audiences:

  * the trading engine (strategy / daemon)     -> POST /intents,
                                                  POST /intents/{id}/response
                                                  (only used when the shim itself
                                                  owns the store; see below)
  * the ESP32 card (over BLE bridge or direct) -> GET /intents/pending,
                                                  POST /card/register,
                                                  POST /intents/{id}/response
                                                  GET /intel/{ref}

In production the trading engine imports :class:`ApprovalRouter` directly
and holds its own reference to :class:`InMemoryApprovalStore`; the shim's
job is to expose that same store to the card over the network. The paper
scripts use :func:`start_shim_thread` to spin the server in a daemon
thread beside the trading loop.

Wire security: this shim binds to loopback and is intended to sit behind a
BLE-tethered phone (which acts as an HTTP proxy) or a Cloudflare / Tailscale
tunnel. It does NOT implement TLS. Do not expose it to the public internet
directly.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sys
import threading
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..brokers.models import OrderAction
from ..intel.vs_engine import DEFAULT_WRITEUP_DIR
from .approval import CardRegistry, InMemoryApprovalStore
from .approval_schemas import PATHS, SCHEMAS
from .router import OrderIntent

# --------------------------------------------------------------------------- #
# OpenAPI spec                                                                #
# --------------------------------------------------------------------------- #

SPEC_VERSION = "1.0.0"


def _build_spec() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "TradeCard Approval Shim",
            "version": SPEC_VERSION,
            "description": (
                "REST wire between the trading engine's ApprovalRouter and a "
                "physical (or simulated) Ed25519 approval card. The card signs "
                "the Prompt.canonical bytes; the shim verifies against a pubkey "
                "registered under card_id. See NEXT_SESSION.md section 11 for "
                "sequencing and remaining gaps."
            ),
            "contact": {"url": "https://github.com/akshan-bansal/FRM-Claude"},
        },
        "servers": [
            {"url": "http://127.0.0.1:8787",
             "description": "Default loopback shim"},
            {"url": "https://tradecard.{host}",
             "description": "Behind a Tailscale / Cloudflare tunnel — shape only",
             "variables": {"host": {"default": "example.ts.net"}}},
        ],
        "security": [{"BearerAuth": []}],
        "components": {
            "securitySchemes": {
                "BearerAuth": {"type": "http", "scheme": "bearer",
                               "description": "Shared secret minted by the paper script "
                                              "when --require-card is on, or supplied to "
                                              "scripts/approval_shim.py via --auth-token."},
            },
            "schemas": SCHEMAS,
        },
        "paths": PATHS,
    }


_SPEC_JSON: bytes = json.dumps(_build_spec(), separators=(",", ":")).encode("utf-8")
_SPEC_ETAG: str = '"' + hashlib.sha256(_SPEC_JSON).hexdigest()[:16] + '"'


# --------------------------------------------------------------------------- #
# handler                                                                     #
# --------------------------------------------------------------------------- #

def make_handler(
    store: InMemoryApprovalStore,
    registry: CardRegistry,
    *,
    writeup_dir: Path = DEFAULT_WRITEUP_DIR,
    auth_token: str | None = None,
):
    """Build the handler class.

    ``auth_token`` — when set, every route EXCEPT ``GET /healthz`` requires
    ``Authorization: Bearer <token>``. Constant-time comparison. Unset (None)
    means the shim runs open, which is only safe on strict loopback.
    """

    # These stay unversioned so a fresh client can bootstrap (fetch the spec,
    # probe liveness) before knowing which API version to hit. Everything else
    # lives under /v1/…; the OpenAPI paths in approval_schemas.py already
    # carry the prefix.
    _PUBLIC_PATHS = {"/healthz", "/openapi.json"}
    _VERSIONED_PREFIX = "/v1"

    class Handler(BaseHTTPRequestHandler):
        server_version = "TradeCardShim/1.0"

        # -- URL versioning --------------------------------------------- #

        def _normalize_path(self) -> None:
            """Strip the ``/v1`` prefix and log a deprecation warning for
            legacy (unversioned) callers.

            Legacy paths continue to work during the deprecation window so
            firmware and third-party clients aren't broken the day the spec
            bumps to 1.0.0. When the window closes, delete the else branch
            below and the same handler dispatches only ``/v1/…`` traffic.
            """
            if self.path in _PUBLIC_PATHS or self.path.startswith("/openapi.json"):
                return
            if self.path.startswith(_VERSIONED_PREFIX + "/"):
                self.path = self.path[len(_VERSIONED_PREFIX):]
                return
            # Legacy shape — support it, but complain on the way through.
            sys.stderr.write(
                f"[approval-shim] DEPRECATED path {self.path} — clients should "
                f"use {_VERSIONED_PREFIX}{self.path} (SPEC_VERSION {SPEC_VERSION}); "
                f"legacy paths will be removed in a future release.\n"
            )

        # -- auth ------------------------------------------------------- #

        def _authorized(self) -> bool:
            if auth_token is None:
                return True
            if self.path in _PUBLIC_PATHS:
                return True
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return False
            presented = header[len("Bearer "):].strip()
            return hmac.compare_digest(presented, auth_token)

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
            self._normalize_path()
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "auth required"})
                return
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
            if self.path == "/openapi.json":
                # Public. ETag'd; a client that already fetched can 304.
                if self.headers.get("If-None-Match") == _SPEC_ETAG:
                    self.send_response(HTTPStatus.NOT_MODIFIED)
                    self.send_header("ETag", _SPEC_ETAG)
                    self.end_headers()
                    return
                accept = self.headers.get("Accept", "")
                ct = ("application/vnd.oai.openapi+json;version=3.1"
                      if "vnd.oai.openapi" in accept else "application/json")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(_SPEC_JSON)))
                self.send_header("Cache-Control", "public, max-age=300")
                self.send_header("ETag", _SPEC_ETAG)
                self.end_headers()
                self.wfile.write(_SPEC_JSON)
                return
            if self.path.startswith("/passbook"):
                # /passbook?limit=N&offset=M  — newest-first resolved prompts.
                import urllib.parse
                q = urllib.parse.urlparse(self.path).query
                params = urllib.parse.parse_qs(q)
                try:
                    limit = int(params.get("limit", ["50"])[0])
                    offset = int(params.get("offset", ["0"])[0])
                except ValueError:
                    self._send_json(HTTPStatus.BAD_REQUEST,
                                    {"error": "limit and offset must be integers"})
                    return
                entries = [e.to_dict() for e in store.passbook(limit=limit, offset=offset)]
                self._send_json(HTTPStatus.OK, {"entries": entries,
                                                "limit": min(max(limit, 0), 500),
                                                "offset": max(offset, 0)})
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
            self._normalize_path()
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "auth required"})
                return
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

        def do_DELETE(self) -> None:
            self._normalize_path()
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "auth required"})
                return
            if self.path.startswith("/card/"):
                card_id = self.path.split("/", 2)[2].split("?")[0]
                if not card_id:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing card_id"})
                    return
                revoked = registry.revoke(card_id)
                self._send_json(
                    HTTPStatus.OK if revoked else HTTPStatus.NOT_FOUND,
                    {"card_id": card_id, "revoked": revoked},
                )
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "no route"})

    return Handler


# --------------------------------------------------------------------------- #
# runners                                                                     #
# --------------------------------------------------------------------------- #

def run_shim(
    store: InMemoryApprovalStore,
    registry: CardRegistry,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    writeup_dir: Path = DEFAULT_WRITEUP_DIR,
    auth_token: str | None = None,
) -> None:
    handler = make_handler(store, registry, writeup_dir=writeup_dir, auth_token=auth_token)
    httpd = ThreadingHTTPServer((host, port), handler)
    scheme = "http"
    sys.stderr.write(f"approval shim listening on {scheme}://{host}:{port}"
                     f"{' (auth required)' if auth_token else ' (OPEN — loopback only)'}\n")
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
    auth_token: str | None = None,
) -> threading.Thread:
    """Daemon-thread wrapper for :func:`run_shim`.

    ``writeup_dir`` defaults to the current value of the module attribute
    (re-read at call time so tests can monkeypatch it)."""
    resolved = writeup_dir if writeup_dir is not None else DEFAULT_WRITEUP_DIR

    def _serve() -> None:
        try:
            run_shim(store, registry, host=host, port=port,
                     writeup_dir=resolved, auth_token=auth_token)
        except Exception as e:  # pragma: no cover
            sys.stderr.write(f"[approval-shim] died: {e}\n")

    t = threading.Thread(target=_serve, name="approval-shim", daemon=True)
    t.start()
    return t


def mint_auth_token() -> str:
    """A URL-safe 32-byte token — 256 bits of entropy. Fine as a shared secret
    between the paper script's shim thread and the card that pairs with it."""
    return secrets.token_urlsafe(32)
