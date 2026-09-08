"""OpenAPI 3.1 component schemas for the TradeCard approval shim.

Hand-authored literal dicts rather than generated from the dataclasses,
because the small, stable shape reads better this way and avoids pulling
pydantic (or any codegen) in as a dependency. Kept in sync with:

  * :class:`~.approval.Prompt`
  * :class:`~.approval.PassbookEntry`
  * :class:`~.router.OrderIntent`
  * the broker allowlist enforced by ``POST /intents``

Sharp edges captured in the descriptions so codegen clients don't guess:
  * ``canonical`` is the exact byte string signed by the card. The spec
    can only say it's a string; the byte-level format
    (``broker|action|symbol|shares|entry|notional|account|intent_id|nonce``)
    is a wire contract that lives outside OpenAPI.
  * ``signature`` in ``ResponseBody`` is base64 of the RAW 64-byte
    Ed25519 signature — not PEM, not DER, not hex.
  * ``intent_id`` is opaque and must not be parsed by clients.
"""
from __future__ import annotations

# --------------------------------------------------------------------------- #
# component schemas                                                           #
# --------------------------------------------------------------------------- #

_BROKERS = ["ib", "kraken", "questrade"]
_VERDICTS = ["ACCEPT", "DECLINE", "EXPIRED"]
_ACTIONS = ["Buy", "Sell", "BTC", "SShort"]
_MODES = ["paper", "dry-run", "live", "autonomous"]

SCHEMAS: dict[str, dict] = {
    "Broker": {
        "type": "string",
        "enum": _BROKERS,
        "description": "Destination brokerage. IB and Kraken are Canadian-user "
                       "defaults; Questrade is the framework's original path.",
    },
    "Verdict": {
        "type": "string",
        "enum": _VERDICTS,
        "description": "Outcome of a resolved prompt. EXPIRED is set by the "
                       "server when a prompt runs past its TTL without a signed "
                       "response.",
    },
    "OrderAction": {
        "type": "string",
        "enum": _ACTIONS,
        "description": "Direction and covering flag. Matches "
                       "`brokers.models.OrderAction`.",
    },
    "Mode": {"type": "string", "enum": _MODES},

    "ErrorBody": {
        "type": "object",
        "required": ["error"],
        "properties": {"error": {"type": "string"}},
    },

    "HealthzBody": {
        "type": "object",
        "required": ["ok", "pending"],
        "properties": {
            "ok": {"type": "boolean"},
            "pending": {"type": "integer", "minimum": 0,
                        "description": "Count of unresolved prompts."},
        },
    },

    "CardRegisterBody": {
        "type": "object",
        "required": ["card_id", "pubkey_pem"],
        "properties": {
            "card_id": {"type": "string",
                        "description": "Stable identifier the card presents on every "
                                       "request. Registering the same id again replaces "
                                       "the prior pubkey."},
            "pubkey_pem": {"type": "string",
                           "description": "Ed25519 SubjectPublicKeyInfo PEM. Anything else "
                                          "(RSA, raw, DER) is rejected."},
        },
    },
    "CardRegisterResult": {
        "type": "object",
        "required": ["card_id"],
        "properties": {"card_id": {"type": "string"}},
    },
    "CardRevokeResult": {
        "type": "object",
        "required": ["card_id", "revoked"],
        "properties": {
            "card_id": {"type": "string"},
            "revoked": {"type": "boolean",
                        "description": "True if the card was registered before this call."},
        },
    },

    "OrderIntent": {
        "type": "object",
        "required": ["symbol", "action", "shares", "entry", "stop", "strategy",
                     "risk_dollars", "account_number", "broker"],
        "properties": {
            "symbol": {"type": "string"},
            "action": {"$ref": "#/components/schemas/OrderAction"},
            "shares": {"type": "integer", "minimum": 1},
            "entry": {"type": "number"},
            "stop":  {"type": "number"},
            "target": {"type": "number", "nullable": True},
            "strategy": {"type": "string"},
            "risk_dollars": {"type": "number"},
            "account_number": {"type": "string"},
            "symbolId": {"type": "integer", "nullable": True},
            "broker": {"$ref": "#/components/schemas/Broker"},
            "mode": {"$ref": "#/components/schemas/Mode"},
            "ttl_seconds": {"type": "number", "default": 90.0,
                            "description": "Prompt lifetime; the server auto-EXPIREs it "
                                           "after this window without a signed response."},
            "thesis": {"type": "string", "maxLength": 140,
                       "description": "Optional VS-engine narration; rendered on the card."},
            "intel_ref": {"type": "string",
                          "description": "Optional writeup key; GET /intel/{ref} returns the "
                                         "full text."},
        },
    },

    "Prompt": {
        "type": "object",
        "required": ["intent_id", "issued_at", "expires_at", "broker", "symbol",
                     "action", "shares", "entry", "stop", "notional_usd",
                     "risk_dollars", "strategy", "account", "mode", "nonce", "canonical"],
        "properties": {
            "intent_id": {"type": "string",
                          "description": "Opaque; do not parse. The mint format is an "
                                         "implementation detail."},
            "issued_at":  {"type": "string", "format": "date-time"},
            "expires_at": {"type": "string", "format": "date-time"},
            "broker": {"$ref": "#/components/schemas/Broker"},
            "symbol": {"type": "string"},
            "action": {"$ref": "#/components/schemas/OrderAction"},
            "shares": {"type": "integer"},
            "entry":  {"type": "number"},
            "stop":   {"type": "number"},
            "target": {"type": "number", "nullable": True},
            "notional_usd": {"type": "number"},
            "risk_dollars": {"type": "number"},
            "strategy": {"type": "string"},
            "account":  {"type": "string"},
            "mode":     {"$ref": "#/components/schemas/Mode"},
            "thesis":   {"type": "string"},
            "intel_ref": {"type": "string"},
            "nonce":    {"type": "string",
                         "description": "Server-minted per prompt. Included in the canonical "
                                        "bytes so signatures are single-use."},
            "canonical": {
                "type": "string",
                "description": (
                    "Exact string the card must sign. Wire format is "
                    "`broker|action|symbol|shares|entry|notional|account|intent_id|nonce`. "
                    "Sign these bytes as UTF-8; do NOT re-serialize the Prompt JSON, "
                    "and do NOT alter any field before signing. This byte-level contract "
                    "is not enforceable by OpenAPI — a codegen client MUST treat "
                    "`canonical` as opaque and pass it straight into the signer."
                ),
            },
        },
    },
    "PromptsList": {
        "type": "object",
        "required": ["prompts"],
        "properties": {
            "prompts": {"type": "array", "items": {"$ref": "#/components/schemas/Prompt"}},
        },
    },

    "ResponseBody": {
        "type": "object",
        "required": ["decision", "card_id", "signature"],
        "properties": {
            "decision": {"type": "string", "enum": ["ACCEPT", "DECLINE"]},
            "card_id": {"type": "string"},
            "signature": {
                "type": "string",
                "format": "byte",
                "description": (
                    "base64 of the RAW 64-byte Ed25519 signature — no PEM wrapper, "
                    "no DER, no hex. The bytes signed are the Prompt's `canonical` "
                    "field encoded as UTF-8."
                ),
            },
        },
    },
    "RespondResult": {
        "type": "object",
        "required": ["accepted", "intent_id"],
        "properties": {
            "accepted": {"type": "boolean",
                         "description": "True iff the signature verified AND the intent was "
                                        "still live AND not previously consumed."},
            "intent_id": {"type": "string"},
        },
    },

    "PassbookEntry": {
        "type": "object",
        "required": ["intent_id", "resolved_at", "verdict", "broker", "symbol",
                     "action", "shares", "notional_usd", "strategy",
                     "thesis", "intel_ref", "card_id"],
        "properties": {
            "intent_id":    {"type": "string"},
            "resolved_at":  {"type": "string", "format": "date-time"},
            "verdict":      {"$ref": "#/components/schemas/Verdict"},
            "broker":       {"$ref": "#/components/schemas/Broker"},
            "symbol":       {"type": "string"},
            "action":       {"$ref": "#/components/schemas/OrderAction"},
            "shares":       {"type": "integer"},
            "notional_usd": {"type": "number"},
            "strategy":     {"type": "string"},
            "thesis":       {"type": "string"},
            "intel_ref":    {"type": "string"},
            "card_id":      {"type": "string", "nullable": True,
                             "description": "Signer for ACCEPT / DECLINE; null for EXPIRED."},
        },
    },
    "PassbookPage": {
        "type": "object",
        "required": ["entries", "limit", "offset"],
        "properties": {
            "entries": {"type": "array",
                        "items": {"$ref": "#/components/schemas/PassbookEntry"}},
            "limit":   {"type": "integer"},
            "offset":  {"type": "integer"},
        },
    },

    "Writeup": {
        "type": "object",
        "description": "Full VS-engine writeup — shape mirrors "
                       "`intel.vs_engine.Writeup.to_dict()`.",
        "properties": {
            "intel_ref":    {"type": "string"},
            "generated_at": {"type": "string", "format": "date-time"},
            "thesis":       {"type": "string"},
            "strategy":     {"type": "string"},
            "symbol":       {"type": "string"},
            "action":       {"$ref": "#/components/schemas/OrderAction"},
            "shares":       {"type": "integer"},
            "broker":       {"$ref": "#/components/schemas/Broker"},
            "reason_clauses":   {"type": "array", "items": {"type": "string"}},
            "overlay_snapshot": {"type": "object", "additionalProperties": {"type": "number"}},
            "market_context":   {"type": "object"},
            "warnings":         {"type": "array", "items": {"type": "string"}},
        },
    },
}


# --------------------------------------------------------------------------- #
# paths                                                                       #
# --------------------------------------------------------------------------- #

_JSON = "application/json"
_ERROR = {"content": {_JSON: {"schema": {"$ref": "#/components/schemas/ErrorBody"}}}}
_AUTH = [{"BearerAuth": []}]


def _resp(schema_ref: str) -> dict:
    return {"content": {_JSON: {"schema": {"$ref": f"#/components/schemas/{schema_ref}"}}}}


PATHS: dict[str, dict] = {
    # Public bootstrap paths — deliberately unversioned so a fresh client can
    # probe liveness and fetch this spec before knowing which API version to
    # use. Every other route lives under /v1/…; legacy (unprefixed) callers
    # still work during the deprecation window but the shim logs a warning
    # on every hit and this spec no longer advertises them.
    "/healthz": {
        "get": {
            "summary": "Liveness probe",
            "description": "Public — no auth. Version-agnostic. Safe for load balancers.",
            "responses": {"200": {"description": "shim is up", **_resp("HealthzBody")}},
        }
    },
    "/openapi.json": {
        "get": {
            "summary": "This spec",
            "description": "Public. Serves the OpenAPI 3.1 document describing the shim. "
                           "Version-agnostic so a client can discover the current wire "
                           "version. Cached with an ETag; supports `If-None-Match`.",
            "responses": {"200": {"description": "the spec"}},
        }
    },
    "/v1/card/register": {
        "post": {
            "summary": "Register or replace a card's pubkey",
            "security": _AUTH,
            "requestBody": {"required": True,
                            "content": {_JSON: {"schema":
                                {"$ref": "#/components/schemas/CardRegisterBody"}}}},
            "responses": {
                "201": {"description": "registered", **_resp("CardRegisterResult")},
                "400": {"description": "bad pubkey PEM", **_ERROR},
                "401": {"description": "auth required", **_ERROR},
            },
        }
    },
    "/v1/card/{card_id}": {
        "delete": {
            "summary": "Revoke a card",
            "description": "After this call, signatures from the old key are refused. "
                           "The card can be re-registered by POSTing a new pubkey.",
            "security": _AUTH,
            "parameters": [{"in": "path", "name": "card_id", "required": True,
                            "schema": {"type": "string"}}],
            "responses": {
                "200": {"description": "revoked", **_resp("CardRevokeResult")},
                "404": {"description": "unknown card", **_resp("CardRevokeResult")},
                "401": {"description": "auth required", **_ERROR},
            },
        }
    },
    "/v1/intents": {
        "post": {
            "summary": "Publish an intent to the card",
            "description": "Only the trading engine should call this. Returns a Prompt "
                           "the card should render and sign.",
            "security": _AUTH,
            "requestBody": {"required": True,
                            "content": {_JSON: {"schema":
                                {"$ref": "#/components/schemas/OrderIntent"}}}},
            "responses": {
                "201": {"description": "published", **_resp("Prompt")},
                "400": {"description": "malformed intent or unsupported broker", **_ERROR},
                "401": {"description": "auth required", **_ERROR},
            },
        }
    },
    "/v1/intents/pending": {
        "get": {
            "summary": "Long-poll for pending prompts",
            "description": "Called by the card every ~1–2 s. Returns every prompt whose "
                           "TTL has not elapsed AND which has not been resolved.",
            "security": _AUTH,
            "responses": {
                "200": {"description": "pending list", **_resp("PromptsList")},
                "401": {"description": "auth required", **_ERROR},
            },
        }
    },
    "/v1/intents/{intent_id}": {
        "get": {
            "summary": "Fetch one pending prompt",
            "security": _AUTH,
            "parameters": [{"in": "path", "name": "intent_id", "required": True,
                            "schema": {"type": "string"},
                            "description": "Opaque; do not parse."}],
            "responses": {
                "200": {"description": "the prompt", **_resp("Prompt")},
                "404": {"description": "unknown or already resolved", **_ERROR},
                "401": {"description": "auth required", **_ERROR},
            },
        }
    },
    "/v1/intents/{intent_id}/response": {
        "post": {
            "summary": "Card responds to a pending intent",
            "description": (
                "Sign the prompt's `canonical` bytes with your card's Ed25519 key "
                "and POST the decision. A signature that verifies against the pubkey "
                "registered under this card_id unblocks the router. A bad signature "
                "does NOT consume the intent — a genuine card can still respond "
                "within TTL. Replays of a consumed intent are refused."
            ),
            "security": _AUTH,
            "parameters": [{"in": "path", "name": "intent_id", "required": True,
                            "schema": {"type": "string"}}],
            "requestBody": {"required": True,
                            "content": {_JSON: {"schema":
                                {"$ref": "#/components/schemas/ResponseBody"}}}},
            "responses": {
                "200": {"description": "verdict recorded", **_resp("RespondResult")},
                "401": {"description": "signature did not verify, or auth missing/wrong",
                        **_resp("RespondResult")},
                "400": {"description": "malformed body (bad base64, unknown decision)",
                        **_ERROR},
            },
        }
    },
    "/v1/intel/{ref}": {
        "get": {
            "summary": "VS-engine writeup for a resolved intent",
            "description": "Rendered by the card's CENTER-button detail view or a companion "
                           "dashboard. Path-traversal guarded.",
            "security": _AUTH,
            "parameters": [{"in": "path", "name": "ref", "required": True,
                            "schema": {"type": "string"},
                            "description": "The `intel_ref` field carried on the Prompt."}],
            "responses": {
                "200": {"description": "the writeup", **_resp("Writeup")},
                "400": {"description": "bad ref", **_ERROR},
                "404": {"description": "no writeup for that ref", **_ERROR},
                "401": {"description": "auth required", **_ERROR},
            },
        }
    },
    "/v1/passbook": {
        "get": {
            "summary": "Newest-first view of resolved verdicts",
            "description": "Companion to the card's on-device passbook. Server-side record "
                           "carries thesis, intel_ref, and card_id of the signer.",
            "security": _AUTH,
            "parameters": [
                {"in": "query", "name": "limit",  "required": False,
                 "schema": {"type": "integer", "default": 50, "maximum": 500, "minimum": 1}},
                {"in": "query", "name": "offset", "required": False,
                 "schema": {"type": "integer", "default": 0, "minimum": 0}},
            ],
            "responses": {
                "200": {"description": "page", **_resp("PassbookPage")},
                "400": {"description": "non-integer limit/offset", **_ERROR},
                "401": {"description": "auth required", **_ERROR},
            },
        }
    },
}
