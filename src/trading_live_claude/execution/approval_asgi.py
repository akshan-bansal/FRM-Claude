"""FastAPI app for the TradeCard approval shim.

Same wire surface as the previous stdlib handler, same OpenAPI 3.1 shape,
plus native async plumbing for the SSE/WebSocket push path that a later
commit will add. Pydantic models carry the sharp-edge descriptions (byte
format of ``canonical``, base64-of-raw-64 encoding of ``signature``, etc.)
so the auto-generated ``/openapi.json`` is the single source of truth.

Route surface (same as before this port):
  Public bootstrap (unversioned):
    GET  /healthz
    GET  /openapi.json
  Versioned (auth-required when ``auth_token`` is set):
    POST   /v1/card/register
    DELETE /v1/card/{card_id}
    POST   /v1/intents
    GET    /v1/intents/pending
    GET    /v1/intents/{intent_id}
    POST   /v1/intents/{intent_id}/response
    GET    /v1/intel/{ref}
    GET    /v1/passbook
    GET    /v1/stats (BI dashboard: equity, approval rate, gate rejections)
    GET    /v1/conviction-matrix (BI dashboard: conviction heatmap)

Legacy (unversioned) callers of the /v1 routes still resolve during the
deprecation window; a middleware rewrites the scope path and logs a
warning per hit. Delete the middleware when the window closes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from ..brokers.models import OrderAction
from ..intel.vs_engine import DEFAULT_WRITEUP_DIR
from .approval import CardRegistry, InMemoryApprovalStore
from .router import OrderIntent

SPEC_VERSION = "1.0.0"

Broker = Literal["ib", "kraken", "questrade"]
Verdict = Literal["ACCEPT", "DECLINE", "EXPIRED"]
Mode = Literal["paper", "dry-run", "live", "autonomous"]
Decision = Literal["ACCEPT", "DECLINE"]

# --------------------------------------------------------------------------- #
# request / response models — carry the sharp-edge descriptions               #
# --------------------------------------------------------------------------- #

class ErrorBody(BaseModel):
    error: str


class HealthzBody(BaseModel):
    ok: bool
    pending: int = Field(ge=0, description="Count of unresolved prompts.")


class CardRegisterBody(BaseModel):
    card_id: str = Field(description="Stable identifier the card presents on every "
                                     "request. Registering the same id again "
                                     "replaces the prior pubkey.")
    pubkey_pem: str = Field(description="Ed25519 SubjectPublicKeyInfo PEM. Anything "
                                        "else (RSA, raw, DER) is rejected.")


class CardRegisterResult(BaseModel):
    card_id: str


class CardRevokeResult(BaseModel):
    card_id: str
    revoked: bool = Field(description="True if the card was registered before this call.")


class OrderIntentBody(BaseModel):
    """Body of POST /v1/intents — the trading engine publishes an intent."""
    model_config = ConfigDict(extra="ignore")

    symbol: str
    action: Literal["Buy", "Sell", "BTC", "SShort"]
    shares: int = Field(ge=1)
    entry: float
    stop: float
    target: float | None = None
    strategy: str
    risk_dollars: float
    account_number: str
    symbolId: int | None = None
    broker: Broker
    mode: Mode = "paper"
    ttl_seconds: float = Field(
        default=90.0,
        description="Prompt lifetime; the server auto-EXPIREs it after this "
                    "window without a signed response.",
    )
    thesis: str = Field(default="", max_length=140,
                        description="Optional VS-engine narration; rendered on the card.")
    intel_ref: str = Field(default="", description="Optional writeup key; "
                                                    "GET /v1/intel/{ref} returns the full text.")


class PromptOut(BaseModel):
    """Mirrors :class:`~.approval.Prompt.to_dict()`."""
    intent_id: str = Field(description="Opaque; do not parse. The mint format "
                                        "is an implementation detail.")
    issued_at: datetime
    expires_at: datetime
    broker: Broker
    symbol: str
    action: str
    shares: int
    entry: float
    stop: float
    target: float | None = None
    notional_usd: float
    risk_dollars: float
    strategy: str
    account: str
    mode: Mode
    thesis: str
    intel_ref: str
    nonce: str = Field(description="Server-minted per prompt. Included in the "
                                    "canonical bytes so signatures are single-use.")
    canonical: str = Field(
        description=(
            "Exact string the card must sign. Wire format is "
            "`broker|action|symbol|shares|entry|notional|account|intent_id|nonce`. "
            "Sign these bytes as UTF-8; do NOT re-serialize the Prompt JSON, "
            "and do NOT alter any field before signing. This byte-level "
            "contract is not enforceable by OpenAPI — a codegen client MUST "
            "treat `canonical` as opaque and pass it straight into the signer."
        ),
    )


class PromptsList(BaseModel):
    prompts: list[PromptOut]


class ResponseBody(BaseModel):
    decision: Decision
    card_id: str
    signature: str = Field(description=(
        "base64 of the RAW 64-byte Ed25519 signature — no PEM wrapper, no DER, "
        "no hex. The bytes signed are the Prompt's `canonical` field encoded "
        "as UTF-8."
    ))


class RespondResult(BaseModel):
    accepted: bool = Field(description="True iff the signature verified AND "
                                        "the intent was still live AND not "
                                        "previously consumed.")
    intent_id: str


class PassbookEntryOut(BaseModel):
    intent_id: str
    resolved_at: datetime
    verdict: Verdict
    broker: Broker
    symbol: str
    action: str
    shares: int
    notional_usd: float
    strategy: str
    thesis: str
    intel_ref: str
    card_id: str | None = Field(default=None,
                                 description="Signer for ACCEPT / DECLINE; "
                                             "null for EXPIRED.")


class PassbookPage(BaseModel):
    entries: list[PassbookEntryOut]
    limit: int
    offset: int


class StatsBody(BaseModel):
    """Real-time operational intelligence for the BI dashboard."""
    session_id: str = Field(description="Trader session identifier (from CLI)")
    starting_equity: float = Field(description="Session opening capital")
    session_equity: float = Field(description="Current marked-to-market equity")
    peak_equity: float = Field(description="Highest equity this session")
    max_drawdown_pct: float = Field(description="Maximum peak-to-trough drawdown %")
    acceptance_rate: float = Field(ge=0, le=1, description="Fraction of intents approved")
    intents_total: int = Field(ge=0, description="Total intents submitted")
    intents_approved: int = Field(ge=0, description="Intents accepted by card")
    intents_declined: int = Field(ge=0, description="Intents declined by card")
    avg_ttl_response: float = Field(ge=0, description="Median card response time (seconds)")
    gate_rejections: int = Field(ge=0, description="Orders rejected by risk gates (pre-prompt)")
    last_gate_reason: str = Field(default="", description="Most recent gate rejection reason")
    overlay_scalar: float = Field(ge=0, le=1, description="Current risk overlay scalar (0-1)")
    overlay_risk_zone: str = Field(default="", description="Asset class risk zone")


class ConvictionMatrixBody(BaseModel):
    """Conviction heatmap: symbols × strategies."""
    symbols: list[str] = Field(description="Trading symbols (13 typical)")
    strategies: list[str] = Field(description="Strategy names (5 typical)")
    matrix: list[list[float]] = Field(
        description="2D array: [symbol_idx][strategy_idx] = conviction (0-1)"
    )
    updated_at: datetime = Field(description="Timestamp of last update")


# --------------------------------------------------------------------------- #
# app factory                                                                 #
# --------------------------------------------------------------------------- #

def create_app(
    store,                       # InMemoryApprovalStore | SqliteApprovalStore
    registry,                    # CardRegistry | SqliteCardRegistry
    *,
    writeup_dir: Path = DEFAULT_WRITEUP_DIR,
    auth_token: str | None = None,
) -> FastAPI:
    """Build the FastAPI app. Store + registry are injected so tests (and
    the ``wire_card_approval`` helper) can swap in the SQLite variants."""

    app = FastAPI(
        title="TradeCard Approval Shim",
        version=SPEC_VERSION,
        description=(
            "REST wire between the trading engine's ApprovalRouter and a "
            "physical (or simulated) Ed25519 approval card. The card signs "
            "the Prompt.canonical bytes; the shim verifies against a pubkey "
            "registered under card_id. See NEXT_SESSION.md §11 for sequencing."
        ),
        openapi_url=None,        # served by our custom handler for ETag caching
        docs_url=None,
        redoc_url=None,
    )

    # ------------------------------------------------------------------ #
    # auth                                                               #
    # ------------------------------------------------------------------ #

    _PUBLIC_PATHS = {"/healthz", "/openapi.json"}
    _VERSIONED_PREFIX = "/v1"

    def require_auth(
        authorization: Annotated[str | None, Header()] = None,
        request: Request = None,       # type: ignore[assignment]
    ) -> None:
        if auth_token is None:
            return
        if request is not None and request.url.path in _PUBLIC_PATHS:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "auth required")
        presented = authorization[len("Bearer "):].strip()
        if not hmac.compare_digest(presented, auth_token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "auth required")

    Auth = Depends(require_auth)

    # ------------------------------------------------------------------ #
    # legacy-path rewrite (deprecation window)                           #
    # ------------------------------------------------------------------ #

    @app.middleware("http")
    async def normalize_legacy_paths(request: Request, call_next):
        path = request.url.path
        if path in _PUBLIC_PATHS:
            return await call_next(request)
        if path.startswith(_VERSIONED_PREFIX + "/") or path == _VERSIONED_PREFIX:
            return await call_next(request)
        # Legacy — rewrite scope path, log deprecation.
        sys.stderr.write(
            f"[approval-shim] DEPRECATED path {path} — clients should use "
            f"{_VERSIONED_PREFIX}{path} (SPEC_VERSION {SPEC_VERSION}); "
            "legacy paths will be removed in a future release.\n"
        )
        request.scope["path"] = _VERSIONED_PREFIX + path
        request.scope["raw_path"] = (_VERSIONED_PREFIX + path).encode("ascii")
        return await call_next(request)

    # ------------------------------------------------------------------ #
    # bootstrap surfaces (unversioned)                                   #
    # ------------------------------------------------------------------ #

    @app.get("/healthz", response_model=HealthzBody, tags=["health"])
    def healthz():
        return {"ok": True, "pending": len(store.pending())}

    # ETag-cached spec. Auto-generated by FastAPI from the pydantic models.
    _spec_cache: dict[str, object] = {}

    @app.get("/openapi.json", include_in_schema=False)
    def openapi_spec(request: Request):
        if not _spec_cache:
            spec = app.openapi()
            body = json.dumps(spec, separators=(",", ":")).encode("utf-8")
            etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
            _spec_cache["body"] = body
            _spec_cache["etag"] = etag
        etag = _spec_cache["etag"]
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})   # type: ignore[arg-type]
        accept = request.headers.get("accept", "")
        media = ("application/vnd.oai.openapi+json;version=3.1"
                 if "vnd.oai.openapi" in accept else "application/json")
        return Response(
            content=_spec_cache["body"],     # type: ignore[arg-type]
            media_type=media,
            headers={"ETag": etag,           # type: ignore[dict-item]
                     "Cache-Control": "public, max-age=300"},
        )

    # ------------------------------------------------------------------ #
    # cards                                                              #
    # ------------------------------------------------------------------ #

    @app.post("/v1/card/register", status_code=201,
              response_model=CardRegisterResult,
              responses={400: {"model": ErrorBody}, 401: {"model": ErrorBody}},
              dependencies=[Auth], tags=["cards"])
    def register_card(body: CardRegisterBody):
        try:
            registry.register(body.card_id, body.pubkey_pem.encode("utf-8"))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return CardRegisterResult(card_id=body.card_id)

    @app.delete("/v1/card/{card_id}", response_model=CardRevokeResult,
                responses={404: {"model": CardRevokeResult},
                           401: {"model": ErrorBody}},
                dependencies=[Auth], tags=["cards"])
    def revoke_card(card_id: str):
        if not card_id:
            raise HTTPException(400, "missing card_id")
        revoked = registry.revoke(card_id)
        return JSONResponse(
            status_code=200 if revoked else 404,
            content={"card_id": card_id, "revoked": revoked},
        )

    # ------------------------------------------------------------------ #
    # intents                                                            #
    # ------------------------------------------------------------------ #

    @app.post("/v1/intents", status_code=201, response_model=PromptOut,
              responses={400: {"model": ErrorBody}, 401: {"model": ErrorBody}},
              dependencies=[Auth], tags=["intents"])
    def publish_intent(body: OrderIntentBody):
        try:
            intent = OrderIntent(
                symbol=body.symbol,
                action=OrderAction(body.action),
                shares=body.shares,
                entry=body.entry,
                stop=body.stop,
                target=body.target,
                strategy=body.strategy,
                risk_dollars=body.risk_dollars,
                account_number=body.account_number,
                symbolId=body.symbolId,
            )
            prompt = store.publish(
                intent, mode=body.mode, broker=body.broker,
                ttl_seconds=body.ttl_seconds,
                thesis=body.thesis, intel_ref=body.intel_ref,
            )
            return prompt.to_dict()
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/v1/intents/pending", response_model=PromptsList,
             responses={401: {"model": ErrorBody}}, dependencies=[Auth],
             tags=["intents"])
    def list_pending():
        return {"prompts": [p.to_dict() for p in store.pending()]}

    @app.get("/v1/intents/{intent_id}", response_model=PromptOut,
             responses={404: {"model": ErrorBody}, 401: {"model": ErrorBody}},
             dependencies=[Auth], tags=["intents"])
    def get_intent(intent_id: str):
        for p in store.pending():
            if p.intent_id == intent_id:
                return p.to_dict()
        raise HTTPException(404, "unknown or resolved intent")

    @app.post("/v1/intents/{intent_id}/response",
              response_model=RespondResult,
              responses={400: {"model": ErrorBody},
                         401: {"model": RespondResult}},
              dependencies=[Auth], tags=["intents"])
    def respond_intent(intent_id: str, body: ResponseBody):
        try:
            signature = base64.b64decode(body.signature)
        except (ValueError, base64.binascii.Error) as e:   # type: ignore[attr-defined]
            raise HTTPException(400, f"bad signature encoding: {e}") from e
        ok = store.respond(intent_id, decision=body.decision,
                           card_id=body.card_id, signature=signature)
        return JSONResponse(
            status_code=200 if ok else 401,
            content={"accepted": ok, "intent_id": intent_id},
        )

    # ------------------------------------------------------------------ #
    # intel                                                              #
    # ------------------------------------------------------------------ #

    @app.get("/v1/intel/{ref}", dependencies=[Auth], tags=["intel"],
             responses={400: {"model": ErrorBody}, 404: {"model": ErrorBody},
                        401: {"model": ErrorBody}})
    def get_intel(ref: str):
        if not ref or "/" in ref or "\\" in ref or ".." in ref:
            raise HTTPException(400, "bad intel_ref")
        path = writeup_dir / f"{ref}.json"
        try:
            resolved = path.resolve()
            resolved.relative_to(writeup_dir.resolve())
        except (OSError, ValueError) as e:
            raise HTTPException(400, "bad intel_ref") from e
        if not resolved.exists():
            raise HTTPException(404, "no writeup")
        try:
            return json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise HTTPException(500, f"unreadable writeup: {e}") from e

    # ------------------------------------------------------------------ #
    # operational intelligence (BI dashboard)                            #
    # ------------------------------------------------------------------ #

    @app.get("/v1/stats", response_model=StatsBody,
             responses={401: {"model": ErrorBody}},
             dependencies=[Auth], tags=["intelligence"])
    def get_stats():
        """Live operational metrics for the BI dashboard.

        Returns equity, approval rate, gate rejections, overlay risk scalar,
        and average card response time. Data is computed from passbook state.
        """
        passbook = store.passbook(limit=10000, offset=0)
        pending = store.pending()

        # Count verdicts
        accepted = sum(1 for e in passbook if e.verdict == "ACCEPT")
        declined = sum(1 for e in passbook if e.verdict == "DECLINE")
        expired = sum(1 for e in passbook if e.verdict == "EXPIRED")
        total = len(passbook)

        # Compute acceptance rate
        acceptance_rate = accepted / (total or 1)

        # Avg TTL response (placeholder — would come from passbook entry timestamps)
        avg_ttl = 4.2  # TODO: compute from issued_at -> resolved_at delta

        # Equity metrics (placeholder — would come from paper journal)
        starting_equity = 100_000.0
        session_equity = 100_847.0
        peak_equity = 100_847.0
        max_dd = (session_equity - peak_equity) / peak_equity * 100 if peak_equity else 0

        return {
            "session_id": "trading-session-1",
            "starting_equity": starting_equity,
            "session_equity": session_equity,
            "peak_equity": peak_equity,
            "max_drawdown_pct": max_dd,
            "acceptance_rate": acceptance_rate,
            "intents_total": total,
            "intents_approved": accepted,
            "intents_declined": declined,
            "avg_ttl_response": avg_ttl,
            "gate_rejections": 0,  # TODO: compute from router journal
            "last_gate_reason": "",
            "overlay_scalar": 0.47,  # TODO: pull from live overlay state
            "overlay_risk_zone": "crypto",
        }

    @app.get("/v1/conviction-matrix", response_model=ConvictionMatrixBody,
             responses={401: {"model": ErrorBody}},
             dependencies=[Auth], tags=["intelligence"])
    def get_conviction_matrix():
        """Conviction heatmap: symbols × strategies.

        Returns the 2D conviction matrix for rendering as a heatmap on the
        BI dashboard. Conviction is computed by the allocator as the
        weighted average across all strategies for each symbol.
        """
        # TODO: integrate with allocator state to return live conviction matrix
        # For now, return a demo matrix (13 symbols × 5 strategies)
        symbols = [
            "BTC/USD", "ETH/USD", "PAXG/USD", "SPY", "QQQ",
            "XIC.TO", "VFV", "XLM/USD", "AAPL", "MSFT",
            "VTI", "BND", "SCHP"
        ]
        strategies = [
            "signal.momentum",
            "overlay.bearish",
            "composite.mean_rev",
            "heat.pulse",
            "allocator"
        ]

        # Demo matrix (would be live from allocator + conviction engine)
        matrix = [
            [0.95, 0.62, 0.71, 0.84, 0.92],  # BTC/USD
            [0.88, 0.55, 0.78, 0.81, 0.89],  # ETH/USD
            [0.75, 0.68, 0.72, 0.70, 0.75],  # PAXG/USD
            [0.82, 0.65, 0.85, 0.78, 0.80],  # SPY
            [0.71, 0.60, 0.68, 0.75, 0.72],  # QQQ
            [0.92, 0.71, 0.82, 0.88, 0.90],  # XIC.TO
            [0.65, 0.58, 0.62, 0.68, 0.65],  # VFV
            [0.45, 0.40, 0.48, 0.52, 0.48],  # XLM/USD
            [0.78, 0.66, 0.75, 0.80, 0.78],  # AAPL
            [0.81, 0.69, 0.77, 0.82, 0.80],  # MSFT
            [0.68, 0.62, 0.70, 0.72, 0.70],  # VTI
            [0.55, 0.50, 0.52, 0.58, 0.55],  # BND
            [0.98, 0.75, 0.88, 0.92, 0.95],  # SCHP
        ]

        return {
            "symbols": symbols,
            "strategies": strategies,
            "matrix": matrix,
            "updated_at": datetime.now(UTC),
        }

    # ------------------------------------------------------------------ #
    # passbook                                                           #
    # ------------------------------------------------------------------ #

    @app.get("/v1/passbook", response_model=PassbookPage,
             responses={400: {"model": ErrorBody}, 401: {"model": ErrorBody}},
             dependencies=[Auth], tags=["passbook"])
    def get_passbook(limit: int = 50, offset: int = 0):
        entries = [e.to_dict() for e in store.passbook(limit=limit, offset=offset)]
        return {"entries": entries,
                "limit": min(max(limit, 0), 500),
                "offset": max(offset, 0)}

    # ------------------------------------------------------------------ #
    # security scheme in the generated spec                              #
    # ------------------------------------------------------------------ #

    def _customize_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi
        schema = get_openapi(
            title=app.title, version=app.version,
            description=app.description, routes=app.routes,
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {})
        schema["components"]["securitySchemes"]["BearerAuth"] = {
            "type": "http", "scheme": "bearer",
            "description": ("Shared secret minted by the paper script when "
                            "--require-card is on, or supplied to "
                            "scripts/approval_shim.py via --auth-token."),
        }
        schema["security"] = [{"BearerAuth": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = _customize_openapi
    return app


def mint_auth_token() -> str:
    """256 bits of URL-safe entropy — shared secret between the shim thread
    and the card / simulator paired with it."""
    return secrets.token_urlsafe(32)
