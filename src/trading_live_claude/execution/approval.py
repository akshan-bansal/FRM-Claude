"""Card-approval layer that wraps :class:`Router`.

Convenience helper :func:`wire_card_approval` builds the store, registry, VS
engine and background shim thread in one shot — used by the paper-trading
scripts under the ``--require-card`` flag.


An ``ApprovalRouter`` publishes each risk-passed intent to an
:class:`ApprovalStore`, waits for a card-signed ACCEPT / DECLINE / expiry,
and only then forwards the intent to the underlying router.

The engine here is the server-side counterpart of an ESP32-hosted approval
card. The ESP32:

  1. long-polls (or subscribes over BLE via a phone bridge) for pending
     prompts,
  2. renders the compact prompt on its e-ink display,
  3. reads a user tap on ACCEPT / DECLINE,
  4. signs the intent's ``canonical`` byte string with an Ed25519 key held
     in its secure element,
  5. POSTs the signed response back.

The router NEVER trusts a response whose signature does not verify against
the pubkey the card registered at pairing time, and never trusts a
response for an intent that has expired or already been consumed.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from ..brokers.models import Order
from ..logging_setup import get_logger
from .router import OrderIntent, Router

log = get_logger(__name__)

Verdict = Literal["ACCEPT", "DECLINE", "EXPIRED"]


# --------------------------------------------------------------------------- #
# canonical string                                                            #
# --------------------------------------------------------------------------- #

def canonical_bytes(
    *,
    broker: str,
    action: str,
    symbol: str,
    shares: int,
    entry: float,
    notional_usd: float,
    account: str,
    intent_id: str,
    nonce: str,
) -> bytes:
    """Bytes the card must sign. WYSIWYS: every field a user cares about is in here.

    ``broker`` is included so a MITM cannot silently redirect an approved
    intent from (say) IB to Kraken and back — the destination brokerage is
    part of what the user is approving.
    """
    parts = [
        broker,
        action,
        symbol,
        str(int(shares)),
        f"{float(entry):.4f}",
        f"{float(notional_usd):.2f}",
        account,
        intent_id,
        nonce,
    ]
    return "|".join(parts).encode("utf-8")


# --------------------------------------------------------------------------- #
# data classes                                                                #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Prompt:
    """The payload rendered on the card and delivered by ``GET /intents/pending``."""

    intent_id: str
    issued_at: datetime
    expires_at: datetime
    broker: str            # "ib" | "kraken" | "questrade" — destination brokerage
    symbol: str
    action: str
    shares: int
    entry: float
    stop: float
    target: float | None
    notional_usd: float
    risk_dollars: float
    strategy: str
    account: str
    mode: str
    thesis: str            # short natural-language reason for the trade (<=140 chars)
    intel_ref: str         # opaque id for full VS investment-engine writeup
    nonce: str
    canonical: str

    def to_dict(self) -> dict[str, object]:
        return {
            "intent_id": self.intent_id,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "broker": self.broker,
            "symbol": self.symbol,
            "action": self.action,
            "shares": self.shares,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "notional_usd": self.notional_usd,
            "risk_dollars": self.risk_dollars,
            "strategy": self.strategy,
            "account": self.account,
            "mode": self.mode,
            "thesis": self.thesis,
            "intel_ref": self.intel_ref,
            "nonce": self.nonce,
            "canonical": self.canonical,
        }


@dataclass
class _PendingEntry:
    prompt: Prompt
    event: threading.Event = field(default_factory=threading.Event)
    verdict: Verdict | None = None
    consumed: bool = False


# --------------------------------------------------------------------------- #
# signature verification                                                      #
# --------------------------------------------------------------------------- #

class CardRegistry:
    """Maps card_id → Ed25519 public key. Populated at pairing time."""

    def __init__(self) -> None:
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._lock = threading.Lock()

    def register(self, card_id: str, pubkey_pem: bytes) -> None:
        key = load_pem_public_key(pubkey_pem)
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("card pubkey must be Ed25519")
        with self._lock:
            self._keys[card_id] = key
        log.info("approval.card_registered", card_id=card_id)

    def verify(self, card_id: str, canonical: bytes, signature: bytes) -> bool:
        with self._lock:
            key = self._keys.get(card_id)
        if key is None:
            log.warning("approval.unknown_card", card_id=card_id)
            return False
        try:
            key.verify(signature, canonical)
            return True
        except InvalidSignature:
            log.warning("approval.bad_signature", card_id=card_id)
            return False


# --------------------------------------------------------------------------- #
# store                                                                       #
# --------------------------------------------------------------------------- #

class ApprovalStore(Protocol):
    def publish(
        self,
        intent: OrderIntent,
        *,
        mode: str,
        broker: str,
        ttl_seconds: float,
        thesis: str = ...,
        intel_ref: str = ...,
    ) -> Prompt: ...
    def wait(self, intent_id: str, *, timeout: float | None = None) -> Verdict: ...
    def pending(self) -> list[Prompt]: ...
    def respond(
        self,
        intent_id: str,
        *,
        decision: Literal["ACCEPT", "DECLINE"],
        card_id: str,
        signature: bytes,
    ) -> bool: ...


class InMemoryApprovalStore:
    """Thread-safe, single-process store. Fine for a laptop shim + one card."""

    def __init__(self, registry: CardRegistry) -> None:
        self._registry = registry
        self._entries: dict[str, _PendingEntry] = {}
        self._lock = threading.Lock()

    # -- helpers ---------------------------------------------------------- #

    @staticmethod
    def _mint_intent_id() -> str:
        # Time-prefixed hex so pending lists sort naturally on the card.
        return f"{int(time.time_ns()):016x}-{secrets.token_hex(6)}"

    def _sweep_expired(self, now: datetime) -> None:
        for entry in list(self._entries.values()):
            if entry.verdict is None and entry.prompt.expires_at <= now:
                entry.verdict = "EXPIRED"
                entry.event.set()

    # -- ApprovalStore ---------------------------------------------------- #

    def publish(
        self,
        intent: OrderIntent,
        *,
        mode: str,
        broker: str,
        ttl_seconds: float,
        thesis: str = "",
        intel_ref: str = "",
    ) -> Prompt:
        now = datetime.now(UTC)
        intent_id = self._mint_intent_id()
        nonce = secrets.token_hex(16)
        notional = float(intent.shares) * float(intent.entry)
        canonical = canonical_bytes(
            broker=broker,
            action=intent.action.value,
            symbol=intent.symbol,
            shares=intent.shares,
            entry=intent.entry,
            notional_usd=notional,
            account=intent.account_number,
            intent_id=intent_id,
            nonce=nonce,
        ).decode("utf-8")
        prompt = Prompt(
            intent_id=intent_id,
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            broker=broker,
            symbol=intent.symbol,
            action=intent.action.value,
            shares=intent.shares,
            entry=intent.entry,
            stop=intent.stop,
            target=intent.target,
            notional_usd=notional,
            risk_dollars=intent.risk_dollars,
            strategy=intent.strategy,
            account=intent.account_number,
            mode=mode,
            thesis=thesis[:140],
            intel_ref=intel_ref,
            nonce=nonce,
            canonical=canonical,
        )
        with self._lock:
            self._sweep_expired(now)
            self._entries[intent_id] = _PendingEntry(prompt=prompt)
        log.info("approval.published", intent_id=intent_id, symbol=intent.symbol,
                 shares=intent.shares, ttl=ttl_seconds)
        return prompt

    def wait(self, intent_id: str, *, timeout: float | None = None) -> Verdict:
        with self._lock:
            entry = self._entries.get(intent_id)
        if entry is None:
            raise KeyError(intent_id)

        remaining = (entry.prompt.expires_at - datetime.now(UTC)).total_seconds()
        wait_for = remaining if timeout is None else min(remaining, timeout)
        wait_for = max(wait_for, 0.0)
        entry.event.wait(timeout=wait_for)

        with self._lock:
            if entry.verdict is None:
                # Timed out; treat as expired if the prompt is past its window.
                if entry.prompt.expires_at <= datetime.now(UTC):
                    entry.verdict = "EXPIRED"
                else:
                    # Caller's own timeout, prompt still live — surface as EXPIRED
                    # to the router (it will journal and drop the intent).
                    entry.verdict = "EXPIRED"
                entry.event.set()
            return entry.verdict

    def pending(self) -> list[Prompt]:
        now = datetime.now(UTC)
        with self._lock:
            self._sweep_expired(now)
            return [e.prompt for e in self._entries.values() if e.verdict is None]

    def respond(
        self,
        intent_id: str,
        *,
        decision: Literal["ACCEPT", "DECLINE"],
        card_id: str,
        signature: bytes,
    ) -> bool:
        with self._lock:
            entry = self._entries.get(intent_id)
            if entry is None:
                log.warning("approval.response_unknown_intent", intent_id=intent_id)
                return False
            if entry.consumed or entry.verdict is not None:
                log.warning("approval.response_replay", intent_id=intent_id,
                            prior_verdict=entry.verdict)
                return False
            if entry.prompt.expires_at <= datetime.now(UTC):
                entry.verdict = "EXPIRED"
                entry.event.set()
                return False

        canonical = entry.prompt.canonical.encode("utf-8")
        if not self._registry.verify(card_id, canonical, signature):
            # Do NOT consume the entry; a genuine card can still respond.
            return False

        with self._lock:
            entry.consumed = True
            entry.verdict = decision
            entry.event.set()
        log.info("approval.response", intent_id=intent_id, decision=decision, card_id=card_id)
        return True


# --------------------------------------------------------------------------- #
# router wrapper                                                              #
# --------------------------------------------------------------------------- #

ThesisFn = "Callable[[OrderIntent, str], tuple[str, str]]"  # (intent, broker) -> (thesis, intel_ref)


class ApprovalRouter:
    """Wraps a :class:`Router` and requires a card-signed ACCEPT before dispatch.

    Ordering matters: risk gates run FIRST. We never prompt the user for
    something the router would reject anyway.

    If ``thesis_fn`` is supplied, it is called after the gates pass and
    before the prompt is published. It should return ``(thesis, intel_ref)``
    — see :mod:`trading_live_claude.intel.vs_engine` for the default
    implementation. Any exception raised by ``thesis_fn`` is swallowed and
    the prompt is published with empty intel fields; the trading loop
    should not fail because the narrator is unavailable.
    """

    def __init__(
        self,
        inner: Router,
        *,
        store: ApprovalStore,
        ttl_seconds: float = 90.0,
        thesis_fn: object | None = None,
    ) -> None:
        if inner.mode == "autonomous":
            # Autonomous == no human loop. Card approval is incompatible.
            raise ValueError(
                "ApprovalRouter cannot wrap an autonomous Router; pick one loop."
            )
        self.inner = inner
        self.store = store
        self.ttl_seconds = ttl_seconds
        self.thesis_fn = thesis_fn

    def submit(
        self,
        intent: OrderIntent,
        *,
        equity: float,
        existing_risk: float,
        open_positions: int,
    ) -> Order | None:
        decision = self.inner._gate(
            intent,
            equity=equity,
            existing_risk=existing_risk,
            open_positions=open_positions,
        )
        if not decision.accepted:
            self.inner.journal.order_intent(
                {
                    "mode": self.inner.mode,
                    "strategy": intent.strategy,
                    "symbol": intent.symbol,
                    "action": intent.action.value,
                    "shares": intent.shares,
                    "entry": intent.entry,
                    "stop": intent.stop,
                    "target": intent.target,
                    "risk_dollars": intent.risk_dollars,
                    "accepted": False,
                    "rejected_reasons": decision.rejected_reasons,
                    "via": "approval-router",
                }
            )
            self.inner.journal.rejected(
                {"symbol": intent.symbol, "reasons": decision.rejected_reasons}
            )
            log.warning("approval_router.rejected_pre_prompt",
                        symbol=intent.symbol, reasons=decision.rejected_reasons)
            return None

        thesis, intel_ref = "", ""
        if self.thesis_fn is not None:
            try:
                thesis, intel_ref = self.thesis_fn(intent, self.inner.broker.name)  # type: ignore[operator]
            except Exception as e:  # narrator failure must not break the loop
                log.warning("approval_router.thesis_fn_failed",
                            symbol=intent.symbol, error=str(e))
        prompt = self.store.publish(
            intent,
            mode=self.inner.mode,
            broker=self.inner.broker.name,
            ttl_seconds=self.ttl_seconds,
            thesis=thesis,
            intel_ref=intel_ref,
        )
        verdict = self.store.wait(prompt.intent_id)
        if verdict != "ACCEPT":
            self.inner.journal.rejected(
                {"symbol": intent.symbol, "reasons": [f"card:{verdict.lower()}"]}
            )
            log.info("approval_router.not_accepted",
                     symbol=intent.symbol, verdict=verdict, intent_id=prompt.intent_id)
            return None

        # Re-runs gates cheaply; guards against races (heat changed while user
        # was thinking, kill-switch tripped, etc.). If it now fails, the
        # underlying router journals the rejection.
        return self.inner.submit(
            intent,
            equity=equity,
            existing_risk=existing_risk,
            open_positions=open_positions,
        )


# --------------------------------------------------------------------------- #
# convenience wiring for paper/live scripts                                   #
# --------------------------------------------------------------------------- #

@dataclass
class CardWiring:
    """The pieces the paper scripts need to hold onto when --require-card is on."""

    router: "ApprovalRouter"
    store: "InMemoryApprovalStore"
    registry: "CardRegistry"
    shim_thread: "threading.Thread | None"
    shim_url: str


def wire_card_approval(
    inner: Router,
    *,
    shim_host: str = "127.0.0.1",
    shim_port: int = 8787,
    ttl_seconds: float = 90.0,
    start_shim: bool = True,
    thesis_fn: object | None = None,
) -> CardWiring:
    """Build the card-approval layer around ``inner`` and (optionally) spin the shim.

    When ``start_shim`` is True, an HTTP server (from
    :mod:`.approval_server`) is launched in a daemon thread so callers get
    the wire surface for free. Pass ``start_shim=False`` for tests or when
    the store is being exposed some other way.
    """
    registry = CardRegistry()
    store = InMemoryApprovalStore(registry)
    router = ApprovalRouter(inner, store=store, ttl_seconds=ttl_seconds, thesis_fn=thesis_fn)

    shim_thread: threading.Thread | None = None
    if start_shim:
        # Local import so ``execution.approval`` stays importable in contexts
        # (mypy runs, docs) where the HTTP layer isn't needed.
        from .approval_server import start_shim_thread
        shim_thread = start_shim_thread(store, registry, shim_host, shim_port)

    return CardWiring(
        router=router,
        store=store,
        registry=registry,
        shim_thread=shim_thread,
        shim_url=f"http://{shim_host}:{shim_port}",
    )
