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
import socket
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from ..brokers.models import OrderAction
from ..intel.vs_engine import DEFAULT_WRITEUP_DIR
from .router import OrderIntent

SPEC_VERSION = "1.0.0"
DEFAULT_DESK_PAGE = Path(__file__).resolve().parents[3] / "pwa" / "desk.html"

# Destination tags a prompt can carry. These are broker ``.venue`` values, NOT ``.name`` values
# (IBBroker.name is "interactive-brokers", IBWebBroker.name is "interactive-brokers-web").
# 2026-09-17: "paper", "ib_web" and "global" were missing, so every prompt from a PaperBroker-wrapped
# feed — i.e. every paper session, the only mode we run — failed RESPONSE validation on
# GET /v1/intents/pending with a 500, the card never saw the prompt, and it expired unapproved.
# "paper" is the honest value: in paper mode the destination really is the local simulator, and the
# card signs ``broker`` as part of the WYSIWYS canonical, so it must not imply a real venue.
# Note canonical_bytes() types broker as a plain str, so signing was never the constraint.
Broker = Literal["ib", "ib_web", "kraken", "questrade", "paper", "global"]

# Default asset class per venue, used when a caller does not say. IB carries equities AND
# derivatives, so its default is the honest "multi" rather than a guess — a loop that knows which
# sleeve it is running passes ``asset_class`` explicitly.
_VENUE_ASSET_CLASS = {"questrade": "equity", "kraken": "crypto", "ib": "multi", "ib_web": "multi",
                      "paper": "paper", "global": "multi"}


@dataclass(frozen=True)
class BookRef:
    """One book a shim reports on: which venue, which journal session, in which currency.

    A single shim can serve several (one port, three brokers). Books are never summed: see
    ``/v1/books``.
    """

    venue: str
    session_id: str
    currency: str = "USD"
    asset_class: str = ""        # blank -> derived from the venue

    @property
    def resolved_asset_class(self) -> str:
        return self.asset_class or _VENUE_ASSET_CLASS.get(self.venue.lower(), "unknown")


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
    shares: float = Field(gt=0)
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
    shares: float
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


    fingerprint: str = Field(
        description=("Truncated SHA-256 of `canonical`, as `7F3A...91C2`. A HUMAN comparison value: show it beside the intent so a viewer can check the dashboard, the device screen and the audit ledger all refer to the same bytes. Verification always uses the full signature over the full `canonical` string — never this abbreviation. Derive it from THIS record; recomputing it from the other fields defeats the purpose."),
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
    shares: float
    notional_usd: float
    strategy: str
    thesis: str
    intel_ref: str
    card_id: str | None = Field(default=None,
                                 description="Signer for ACCEPT / DECLINE; "
                                             "null for EXPIRED.")
    fingerprint: str = Field(default="", description=("Truncated SHA-256 of `canonical`, as `7F3A...91C2`. A HUMAN comparison value: show it beside the intent so a viewer can check the dashboard, the device screen and the audit ledger all refer to the same bytes. Verification always uses the full signature over the full `canonical` string — never this abbreviation. Derive it from THIS record; recomputing it from the other fields defeats the purpose."))


class PassbookPage(BaseModel):
    entries: list[PassbookEntryOut]
    limit: int
    offset: int


class StatsBody(BaseModel):
    """Real-time operational intelligence for the BI dashboard."""
    session_id: str | None = Field(
        default=None,
        description="Trader session identifier. Null when the shim has no session context — see "
                    "`placeholders`.")
    starting_equity: float | None = Field(
        default=None,
        description="Session opening capital, from the journal's peak for this session. Null when "
                    "the shim cannot identify the session or it has not marked yet.")
    session_equity: float | None = Field(
        default=None,
        description="Equity at the session's latest journalled mark, read from "
                    "state/paper_equity.csv — never recomputed here. Null when unread.")
    peak_equity: float | None = Field(default=None, description="Journalled peak for this session.")
    max_drawdown_pct: float | None = Field(
        default=None,
        description="Drawdown at that mark, as the journal recorded it. Null when unread; a client "
                    "must render null as unknown and never as a flat book.")
    acceptance_rate: float = Field(ge=0, le=1, description="Fraction of intents approved")
    intents_total: int = Field(ge=0, description="Total intents submitted")
    intents_approved: int = Field(ge=0, description="Intents accepted by card")
    intents_declined: int = Field(ge=0, description="Intents declined by card")
    intents_expired: int = Field(default=0, ge=0, description="Prompts that expired unanswered")
    intents_pending: int = Field(default=0, ge=0, description="Prompts awaiting a verdict right now")
    avg_ttl_response: float | None = Field(
        default=None, ge=0,
        description="Median seconds between a prompt being issued and the card answering it, "
                    "over decided prompts only (EXPIRED excluded — its resolved_at is when the "
                    "sweep noticed it). Null when nothing has been decided yet; a client must "
                    "render null as unknown and never as a fast response.")
    gate_rejections: int = Field(ge=0, description="Orders rejected by risk gates (pre-prompt)")
    last_gate_reason: str = Field(default="", description="Most recent gate rejection reason")
    overlay_scalar: float | None = Field(
        default=None, ge=0, le=1,
        description="Current risk overlay scalar (0-1). NULL when the shim has no live overlay "
                    "feed — it is never a stand-in value, so a client must render null as unknown "
                    "rather than as low risk.")
    overlay_risk_zone: str | None = Field(default=None, description="Asset class risk zone, or null.")
    placeholders: list[str] = Field(
        default_factory=list,
        description="Fields this response could not populate from a live source. Anything named "
                    "here is null by design, not missing by accident.")


class ConvictionMatrixBody(BaseModel):
    """Walk-forward scores per (symbol, strategy) pair — a deliberately sparse grid."""
    symbols: list[str] = Field(description="Walk-forward validated symbols, best score first")
    strategies: list[str] = Field(description="The strategies those symbols were validated on")
    matrix: list[list[float | None]] = Field(
        description="2D array: [symbol_idx][strategy_idx] = out-of-sample score for that pair, or "
                    "NULL where the pair was never validated. Null means no evidence, NOT zero "
                    "conviction, and the values are an unbounded ratio — do not render them as a "
                    "0-1 scale.")
    available: bool = Field(default=True, description="False when no validated pair exists to report.")
    source: str = Field(default="", description="Where the numbers come from.")
    metric: str = Field(default="", description="What the numbers are, including their range.")
    note: str = Field(default="", description="How to read the nulls.")
    tiers: dict[str, str] = Field(default_factory=dict,
                                   description="Per-symbol walk-forward tier (robust / watch).")
    updated_at: datetime = Field(description="Timestamp of last update")


# --------------------------------------------------------------------------- #
# app factory                                                                 #
# --------------------------------------------------------------------------- #

class BasketAsk(BaseModel):
    """A venue and the symbols a human typed for it."""
    model_config = ConfigDict(extra="forbid")
    venue: Literal["kraken", "qt", "ib"]
    symbols: list[str] = Field(min_length=1, max_length=60)


def create_app(
    store,                       # InMemoryApprovalStore | SqliteApprovalStore
    registry,                    # CardRegistry | SqliteCardRegistry
    *,
    writeup_dir: Path = DEFAULT_WRITEUP_DIR,
    auth_token: str | None = None,
    desk_page: Path | None = None,   # built desk panel; None -> DEFAULT_DESK_PAGE
    state_dir: Path | None = None,   # journals to read equity and meter readings from
    session_id: str | None = None,   # the book this shim is attached to
    account_currency: str = "USD",
    books: list[BookRef] | None = None,   # every book this shim reports on (one port, N brokers)
    journal=None,                # Optional OrderJournal for metrics
    router=None,                 # Optional Router for metrics
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

    _PUBLIC_PATHS = {"/healthz", "/openapi.json", "/desk"}
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
    # baskets: choose, analyse, propose. The card signs; the panel never does. #
    # ------------------------------------------------------------------ #

    def _basket_files():
        if state_dir is None:
            raise HTTPException(503, "this shim has no state dir, so it cannot read or write baskets")
        return Path(state_dir) / "baskets.jsonl", Path(state_dir) / "basket_proposals.jsonl"

    def _analyse(ask: BasketAsk) -> dict:
        """Run the engines over the typed symbols. Daily closes come only from Kraken's public OHLC,
        for pairs the crypto sleeve maps; every other venue's symbols report no statistics."""
        import pandas as pd

        from ..analysis.basket_report import build_report
        from ..analysis.universe import CRYPTO_SLEEVE
        from ..data.kraken_ohlc import kraken_ohlc
        from .basket import normalize
        syms = list(normalize(ask.symbols))
        closes: dict = {}
        fetch_errors: dict[str, str] = {}
        if ask.venue == "kraken":
            for sym in syms:
                entry = CRYPTO_SLEEVE.get(sym)
                if entry is None:
                    fetch_errors[sym] = "no Kraken pair mapping in the crypto sleeve"
                    continue
                try:
                    df = kraken_ohlc(entry.pair, interval=1440, timeout=10.0)
                    closes[sym] = pd.Series(df["close"].astype(float).values)
                except Exception as e:  # network/API: report, never invent
                    fetch_errors[sym] = f"Kraken OHLC failed: {e}"
        snapshot = None
        decisions: dict = {}
        journal = Path(state_dir) / "intel_overlay.jsonl" if state_dir else None
        if journal and journal.exists():
            from ..intel.overlay import IntelSnapshot
            lines = [ln for ln in journal.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if lines:
                row = json.loads(lines[-1])
                snapshot = IntelSnapshot(**row["snapshot"])
                decisions = row.get("decisions") or {}
        try:
            seed = json.loads(Path("config/basket_seed.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            seed = {}
        rep = build_report(ask.venue, syms, closes=closes, snapshot=snapshot, overlay_decisions=decisions,
                           asset_class_of=None,
                           launch_map=seed.get("strategy_map", {}).get(ask.venue),
                           fallback_strategy=seed.get("fallback_strategy", {}).get(ask.venue))
        rep["fetch_errors"] = fetch_errors
        rep["generated_at"] = datetime.now().astimezone().isoformat()
        return rep

    @app.get("/v1/basket", dependencies=[Auth], tags=["basket"])
    def get_baskets():
        from .basket import BasketBook, pending_proposals, registry_verifier
        bpath, ppath = _basket_files()
        book = BasketBook(bpath, registry_verifier(Path(state_dir) / "approval.db"))
        venues = {}
        for v in ("kraken", "qt", "ib"):
            ap = book.approved(v)
            venues[v] = None if ap is None else {"symbols": sorted(ap.symbols), "issued_at": ap.issued_at,
                                                 "card_id": ap.card_id, "analysis_hash": ap.analysis_hash}
        return {"venues": venues, "rejected_rows": book.rejected_rows,
                "proposals": [{k: p[k] for k in ("id", "venue", "symbols", "issued_at", "fingerprint",
                                                  "analysis_hash")} for p in pending_proposals(ppath)]}

    @app.post("/v1/basket/analyze", dependencies=[Auth], tags=["basket"])
    def post_basket_analyze(ask: BasketAsk):
        _basket_files()
        return _analyse(ask)

    @app.post("/v1/basket/propose", dependencies=[Auth], tags=["basket"])
    def post_basket_propose(ask: BasketAsk):
        """Analyse, then queue the basket for the card. Nothing is approved by this call: the card
        must sign the fingerprint returned here, via scripts/basket.py on the simulated card."""
        from .basket import append_proposal, make_proposal
        _, ppath = _basket_files()
        rep = _analyse(ask)
        refused = [r["symbol"] for r in rep["rows"] if r["policy_refusal"]]
        if refused:
            raise HTTPException(422, f"venue policy refuses: {', '.join(refused)}")
        prop = make_proposal(venue=ask.venue, symbols=ask.symbols, analysis=rep)
        append_proposal(prop, ppath)
        return {k: prop[k] for k in ("id", "venue", "symbols", "issued_at", "fingerprint", "analysis_hash")}

    # ------------------------------------------------------------------ #
    # IB access: which of IB's listening ports answer right now          #
    # ------------------------------------------------------------------ #

    @app.get("/v1/ib/access", dependencies=[Auth], tags=["ib"])
    def get_ib_access():
        """TCP-probe the ports an IB login opens, measured at request time.

        A connect only shows that something is listening; it says nothing about whether the login
        is the right account, has market-data permissions, or has the API enabled. The panel says
        so. No credential is read, sent or stored, and nothing is ordered.
        """
        from ..config.settings import get_settings
        cfg = get_settings()
        host = cfg.ib_host
        targets = [
            ("TWS paper (socket API)", host, 7497, "paper_trading_workstation"),
            ("TWS live (socket API)", host, 7496, "live_trading_workstation"),
            ("IB Gateway paper (socket API)", host, 4002, "paper_gateway"),
            ("IB Gateway live (socket API)", host, 4001, "live_gateway"),
            ("Client Portal Gateway (web API)", cfg.ib_web_host, cfg.ib_web_port, "client_portal"),
        ]
        rows = []
        for label, h, port, key in targets:
            try:
                with socket.create_connection((h, port), timeout=0.6):
                    up = True
            except OSError:
                up = False
            rows.append({"key": key, "label": label, "host": h, "port": port, "listening": up})
        return {"checked_at": datetime.now().astimezone().isoformat(),
                "configured_socket_port": cfg.ib_paper_port if cfg.ib_use_paper else None,
                "ports": rows}

    # ------------------------------------------------------------------ #
    # operational intelligence (BI dashboard)                            #
    # ------------------------------------------------------------------ #

    @app.get("/v1/stats", response_model=StatsBody,
             responses={401: {"model": ErrorBody}},
             dependencies=[Auth], tags=["intelligence"])
    def get_stats():
        """Live operational metrics for the BI dashboard.

        Returns equity, approval rate, gate rejections, overlay risk scalar,
        and average card response time. Data is computed from passbook state
        and (when available) the router's journal and allocator.
        """
        # Use ApprovalMetrics if journal is available; otherwise compute from store only
        if journal is not None:
            from .approval_metrics import ApprovalMetrics
            metrics = ApprovalMetrics(store, journal, router, state_dir=state_dir,
                                      session_id=session_id,
                                      account_currency=account_currency)
            return metrics.get_stats()

        # No journal: the store alone knows the verdicts, and nothing else. Equity, drawdown,
        # response time and the overlay have no source here, so they are null and named in
        # `placeholders` — this block used to return -0.2 drawdown, 4.2s response and a 0.47 overlay
        # scalar, invented numbers a dashboard could not tell apart from measurements.
        passbook = store.passbook(limit=10000, offset=0)
        accepted = sum(1 for e in passbook if e.verdict == "ACCEPT")
        declined = sum(1 for e in passbook if e.verdict == "DECLINE")
        expired = sum(1 for e in passbook if e.verdict == "EXPIRED")
        decided = accepted + declined

        return {
            "session_id": None,
            "starting_equity": 0.0,
            "session_equity": 0.0,
            "peak_equity": 0.0,
            "max_drawdown_pct": 0.0,
            "acceptance_rate": accepted / (decided or 1) if decided > 0 else 0.0,
            "intents_total": len(passbook),
            "intents_approved": accepted,
            "intents_declined": declined,
            "intents_expired": expired,
            "intents_pending": len(store.pending()),
            "avg_ttl_response": None,
            "gate_rejections": 0,
            "last_gate_reason": "",
            "overlay_scalar": None,
            "overlay_risk_zone": None,
            "placeholders": ["session_id", "starting_equity", "session_equity", "peak_equity",
                             "max_drawdown_pct", "avg_ttl_response", "gate_rejections",
                             "overlay_scalar", "overlay_risk_zone"],
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
        # Use ApprovalMetrics if available; otherwise return demo matrix
        if journal is not None:
            from .approval_metrics import ApprovalMetrics
            metrics = ApprovalMetrics(store, journal, router)
            return metrics.get_conviction_matrix()

        # No journal: the walk-forward registry is still readable, so serve that rather than a
        # hard-coded grid. This block used to return a 13x5 demo matrix including SPY and BTC/USD,
        # neither of which has ever been walk-forward validated.
        import tempfile as _tmp

        from .approval_metrics import ApprovalMetrics
        from .journal import OrderJournal as _J
        return ApprovalMetrics(store, _J(Path(_tmp.gettempdir()) / "frm-shim-nojournal"),
                               router).get_conviction_matrix()


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

    # ------------------------------------------------------------------ #
    # desk panel (unversioned shell + versioned page)                    #
    # ------------------------------------------------------------------ #
    #
    # Serving the built panel from the shim is what makes it live: its fetches become same-origin,
    # so no CORS hole has to be opened and no token ever travels in a URL. The split is deliberate.
    #
    #   GET /desk            public, carries no data — a shell that asks for the token if needed
    #   GET /v1/desk/page    the built page, behind the same auth as every other /v1 route
    #
    # The page is journal-derived (equity, fills, P&L), so it must not be readable by anyone who
    # can merely reach the port. The shell holds the token in sessionStorage and writes the page
    # into the document, which keeps the header on the request that actually fetches the data.

    def _desk_file() -> Path | None:
        page = desk_page if desk_page is not None else DEFAULT_DESK_PAGE
        return page if page.exists() else None


    def _books() -> list[BookRef]:
        """Every book this shim reports on, newest declaration wins for a repeated session."""
        if books:
            return list(books)
        if session_id:
            return [BookRef(venue="unknown", session_id=session_id, currency=account_currency)]
        return []

    @app.get("/v1/books", include_in_schema=False, dependencies=[Auth])
    def books_snapshot() -> Response:
        """Per-book readings, denormalized by brokerage and asset class — panel #1's feed.

        One shim can carry several books (QT equities, Kraken crypto, IB derivatives). This route
        does NOT aggregate them: a single equity number across a CAD equity book and a USD crypto
        book would be a currency-mixed fiction. Each book carries its own venue, asset class,
        currency and meter readings, each reading carrying the file it came from — and a book whose
        journal has no rows yet is returned as UNREAD rather than as zeros.
        """
        refs = _books()
        if state_dir is None or not refs:
            return JSONResponse({
                "schema_version": 1, "books": [],
                "notes": ["This shim was started without books or a state directory, so it cannot "
                          "say which books a reading would describe."]})
        from ..audit.meters import snapshot as _snapshot
        out: list[dict] = []
        for ref in refs:
            row: dict = {"venue": ref.venue, "asset_class": ref.resolved_asset_class,
                         "session_id": ref.session_id, "currency": ref.currency}
            try:
                row["meters"] = _snapshot(Path(state_dir), ref.session_id, ref.currency)
                row["mode"] = row["meters"].get("mode", "LIVE")
            except Exception as exc:   # a contradictory journal is reported, not rounded off
                row["mode"] = "UNREADABLE"
                row["meters"] = {"schema_version": 1, "readings": {},
                                 "notes": [f"{type(exc).__name__}: {exc}"]}
            out.append(row)
        return JSONResponse({"schema_version": 1, "books": out,
                             "notes": [f"{len(out)} book(s); no cross-book aggregate is computed "
                                       "because the books are in different currencies and venues."]})

    @app.get("/v1/meters/snapshot", include_in_schema=False, dependencies=[Auth])
    def meters_snapshot() -> Response:
        """Live readings for the atlas, in the same wire schema an uploaded snapshot uses.

        Same source as ``/v1/stats`` and the same rules: a reading carries the file it came from,
        the scope it covers and when it was observed, or it is not emitted. A shim with no session
        context emits an empty snapshot and says why, rather than readings about an unknown book.
        """
        if state_dir is None or not session_id:
            return JSONResponse({
                "schema_version": 1, "mode": "NO SESSION CONTEXT", "readings": {},
                "notes": ["This shim was started without a session id or state directory, so it "
                          "cannot say which book a reading would describe."]})
        from ..audit.meters import snapshot as _snapshot
        try:
            return JSONResponse(_snapshot(Path(state_dir), session_id, account_currency))
        except Exception as exc:       # a contradictory journal is reported, not rounded off
            return JSONResponse({"schema_version": 1, "mode": "UNREADABLE", "readings": {},
                                 "notes": [f"{type(exc).__name__}: {exc}"]}, status_code=409)

    @app.get("/desk", include_in_schema=False)
    def desk_shell() -> Response:
        cfg = json.dumps({"spec": SPEC_VERSION, "auth_required": auth_token is not None,
                          "poll_seconds": 2})
        shell = (
            "<!doctype html><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>QuantPort.io</title>"
            "<style>body{margin:0;height:100vh;display:grid;place-items:center;background:#0b1417;"
            "color:#e6eef0;font:13px ui-monospace,Menlo,monospace}"
            "form{display:none;gap:8px;margin-top:14px}form.on{display:flex}"
            "input,button{font:inherit;color:#e6eef0;background:#172c35;border:1px solid #213d48;"
            "border-radius:2px;padding:8px 10px}button{cursor:pointer}"
            "p{color:#7d99a3;letter-spacing:.1em}</style>"
            "<main><div style='letter-spacing:.14em'>QUANTPORT<span style='color:#3fb8c4'>.IO</span>"
            "</div><p id=msg>CONNECTING TO THE SHIM…</p>"
            "<form id=f><input id=t type=password placeholder='shim token' autocomplete='off'>"
            "<button>UNLOCK</button></form></main>"
            # Scoped: document.write hands the document to the built page but keeps this Window,
            # so a name declared here is still declared when the page's own script runs — and two
            # `const $` declarations in one scope is a SyntaxError that kills the whole page.
            "<script>(function(){\n"
            f"const CFG={cfg};const KEY='tc.shim.token';\n"
            "const $=(i)=>document.getElementById(i);\n"
            "async function load(){\n"
            "  const tok=sessionStorage.getItem(KEY);\n"
            "  let r;\n"
            "  try{ r=await fetch('/v1/desk/page',{headers:tok?{Authorization:'Bearer '+tok}:{}});}\n"
            "  catch(e){ $('msg').textContent='SHIM UNREACHABLE — '+e.message; return; }\n"
            "  if(r.status===401){ sessionStorage.removeItem(KEY);\n"
            "    $('msg').textContent='THIS SHIM NEEDS ITS TOKEN. The session printed it at launch.';\n"
            "    $('f').className='on'; $('t').focus(); return; }\n"
            "  if(!r.ok){ $('msg').textContent='SHIM RETURNED '+r.status+' — '+(await r.text()).slice(0,120); return; }\n"
            "  const html=await r.text();\n"
            "  const pre='<scr'+'ipt>window.__SHIM__='+JSON.stringify(CFG)+';</scr'+'ipt>';\n"
            "  document.open(); document.write(pre+html); document.close();\n"
            "}\n"
            "$('f').addEventListener('submit',(e)=>{e.preventDefault();\n"
            "  sessionStorage.setItem(KEY,$('t').value.trim()); $('f').className='';\n"
            "  $('msg').textContent='UNLOCKING…'; load();});\n"
            "load();\n"
            "})();</script>"
        )
        return Response(shell, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})

    @app.get("/v1/desk/page", include_in_schema=False, dependencies=[Auth])
    def desk_built_page() -> Response:
        page = _desk_file()
        if page is None:
            expected = desk_page if desk_page is not None else DEFAULT_DESK_PAGE
            return JSONResponse(
                {"error": "the desk panel has not been built",
                 "expected": str(expected),
                 "build": "python scripts/build_desk_dash.py"},
                status_code=404)
        return Response(page.read_text(encoding="utf-8"),
                        media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})


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
