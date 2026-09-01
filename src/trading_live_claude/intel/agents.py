"""OASIS-style specialist reader agents + adversarial thesis debate against the Anthropic API.

Two roles, real LLM calls (no injectable stub):

* :class:`SpecialistReader` — one per domain (``energy``, ``geopolitics``, ``macro``, ``disaster``).
  Reads its slice of the graph (a small evidence bundle: recent events, sources, regions) and
  produces a structured :class:`Claim` — thesis name, direction, confidence self-report, one-line
  inference, cited evidence. The specialist speaks only within its domain; a specialist that
  cannot make a claim from what it sees returns a null-claim rather than fabricating one.

* :class:`Adversary` — challenges a claim by looking for missing corroboration, thin sources,
  contradictory evidence in the same bundle, or overreach relative to the specialist's own
  cited evidence. Returns a :class:`Critique` with a verdict and a one-line reason.

:func:`debate` runs one specialist per domain, then the adversary against each claim, and returns
only the survivors — the ones the adversary marked ``UPHELD``. Claims marked ``FALSIFIED`` are
dropped; ``WEAK`` claims are kept but demoted in confidence. Consistent with the rest of the
intel wing, the surviving output is a **hypothesis** — nothing here is an entry signal.

Why real API calls. The user asked explicitly for no injectable reasoner: the value of the
debate pattern is only real when a genuinely capable model is on both sides. The HTTP surface is
a single ``httpx.Client`` slot that tests inject with a ``respx`` mock — the mocking is at the
transport layer, not the reasoning layer, so the module code itself has no test-only branches.

Failure posture. A missing API key, HTTP error, or unparseable response returns an empty debate
result (no fired theses) and logs a warning — the live intel path must never be brought down by
the agent layer.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import httpx

from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger(__name__)

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

Domain = Literal["energy", "geopolitics", "macro", "disaster"]
Direction = Literal["adverse", "constructive", "neutral"]
Verdict = Literal["UPHELD", "WEAK", "FALSIFIED"]

_DOMAINS: tuple[Domain, ...] = ("energy", "geopolitics", "macro", "disaster")


@dataclass(frozen=True)
class Claim:
    """One structured reading produced by a specialist over its slice of the graph."""

    domain: Domain
    thesis: str
    direction: Direction
    confidence: float          # specialist's self-report [0, 1]
    inference: str
    evidence: list[str] = field(default_factory=list)     # short citations from the bundle


@dataclass(frozen=True)
class Critique:
    """The adversary's challenge to a specific claim."""

    domain: Domain
    verdict: Verdict
    reason: str
    demote_by: float = 0.0     # subtract from confidence when verdict==WEAK; UPHELD -> 0


@dataclass(frozen=True)
class FiredThesis:
    """Post-debate output. ``confidence`` is the specialist's self-report minus any adversary demote."""

    domain: Domain
    thesis: str
    direction: Direction
    confidence: float
    inference: str
    evidence: list[str]
    adversary_verdict: Verdict
    adversary_reason: str


# ---- HTTP surface ------------------------------------------------------------


def _messages_call(
    *,
    system: str,
    user: str,
    client: httpx.Client | None = None,
    max_tokens: int = 700,
) -> str | None:
    """Send one Messages-API request and return the assistant text, or ``None`` on failure.

    Never raises. All failure modes — missing key, HTTP error, malformed response — return None
    so the caller can drop the claim rather than crash the live intel path.
    """
    s = get_settings()
    if not s.anthropic_api_key:
        log.warning("agents.no_anthropic_api_key")
        return None
    body = {
        "model": s.anthropic_model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    headers = {
        "x-api-key": s.anthropic_api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    owns = client is None
    client = client or httpx.Client(timeout=45.0)
    try:
        r = client.post(_ANTHROPIC_URL, headers=headers, json=body)
        payload = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("agents.http_error", error=str(e))
        return None
    finally:
        if owns:
            client.close()
    if r.status_code >= 400:
        log.warning("agents.api_error", status=r.status_code, body=str(payload)[:200])
        return None
    # Response shape: {content: [{type: "text", text: "..."}], ...}
    content = payload.get("content") or []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text") or "")
    return None


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first well-formed JSON object out of the LLM's reply text.

    Models often wrap JSON in prose or fence blocks; grab the first ``{...}`` region and try to
    parse it. Returns None on failure.
    """
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


# ---- specialist reader -------------------------------------------------------


_SPECIALIST_SYSTEM = (
    "You are a specialist reader for the {domain} domain of an OSINT intel feed used by a "
    "trading system. You look ONLY at the evidence bundle you are given, and you speak ONLY "
    "about the {domain} domain. Do NOT recommend trades — your output is hypothesis and posture. "
    "If the evidence does not support a claim, return direction=\"neutral\" and confidence<=0.2.\n\n"
    "Respond with ONE JSON object and no other text:\n"
    "{{\"thesis\": <short name>, \"direction\": \"adverse\"|\"constructive\"|\"neutral\", "
    "\"confidence\": <0..1>, \"inference\": <one sentence>, "
    "\"evidence\": [<short citation>, ...]}}"
)


class SpecialistReader:
    """Reads a domain-scoped evidence bundle and produces a :class:`Claim`."""

    def __init__(self, domain: Domain, *, client: httpx.Client | None = None) -> None:
        if domain not in _DOMAINS:
            raise ValueError(f"unknown domain {domain!r}; expected one of {_DOMAINS}")
        self.domain = domain
        self._client = client

    def read(self, evidence: list[dict[str, Any]], *, as_of: str = "") -> Claim | None:
        """Produce a claim from an evidence bundle. Returns None on any failure."""
        system = _SPECIALIST_SYSTEM.format(domain=self.domain)
        user = (
            f"Domain: {self.domain}. As of: {as_of or 'unspecified'}.\n\n"
            f"Evidence bundle ({len(evidence)} record(s)):\n"
            f"{json.dumps(evidence, default=str, indent=2)[:6000]}\n\n"
            f"Return a single JSON object per the system prompt schema."
        )
        text = _messages_call(system=system, user=user, client=self._client)
        obj = _extract_json(text or "")
        if not obj:
            return None
        try:
            direction = obj.get("direction", "neutral")
            if direction not in ("adverse", "constructive", "neutral"):
                direction = "neutral"
            conf_raw = obj.get("confidence", 0.0)
            confidence = max(0.0, min(1.0, float(conf_raw)))
            return Claim(
                domain=self.domain,
                thesis=str(obj.get("thesis") or "unnamed"),
                direction=direction,       # type: ignore[arg-type]
                confidence=confidence,
                inference=str(obj.get("inference") or ""),
                evidence=[str(x) for x in (obj.get("evidence") or []) if x][:8],
            )
        except (TypeError, ValueError):
            return None


# ---- adversary ---------------------------------------------------------------


_ADVERSARY_SYSTEM = (
    "You are an adversary. Your ONLY job is to challenge a domain specialist's claim against "
    "the SAME evidence they cited plus the broader bundle attached. You are NOT allowed to "
    "argue that the claim is 'right anyway' — either the evidence supports it (UPHELD), it is "
    "thin or overreaching (WEAK), or the same bundle contains a contradiction that falsifies "
    "it (FALSIFIED). Be terse and specific.\n\n"
    "Respond with ONE JSON object and no other text:\n"
    "{{\"verdict\": \"UPHELD\"|\"WEAK\"|\"FALSIFIED\", "
    "\"reason\": <one sentence>, "
    "\"demote_by\": <0..0.5 when verdict=\"WEAK\", else 0>}}"
)


class Adversary:
    """Critiques a :class:`Claim` against the evidence bundle it was drawn from."""

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self._client = client

    def critique(self, claim: Claim, evidence: list[dict[str, Any]]) -> Critique | None:
        user = (
            f"Claim:\n{json.dumps(asdict(claim), default=str, indent=2)}\n\n"
            f"Broader evidence bundle ({len(evidence)} record(s)):\n"
            f"{json.dumps(evidence, default=str, indent=2)[:6000]}\n\n"
            f"Return a single JSON object per the system prompt schema."
        )
        text = _messages_call(system=_ADVERSARY_SYSTEM, user=user, client=self._client)
        obj = _extract_json(text or "")
        if not obj:
            return None
        verdict = obj.get("verdict", "WEAK")
        if verdict not in ("UPHELD", "WEAK", "FALSIFIED"):
            verdict = "WEAK"
        try:
            demote = max(0.0, min(0.5, float(obj.get("demote_by", 0.0) or 0.0)))
        except (TypeError, ValueError):
            demote = 0.0
        if verdict == "UPHELD":
            demote = 0.0
        return Critique(
            domain=claim.domain,
            verdict=verdict,        # type: ignore[arg-type]
            reason=str(obj.get("reason") or ""),
            demote_by=demote,
        )


# ---- debate ------------------------------------------------------------------


def _domain_slice(evidence: list[dict[str, Any]], domain: Domain) -> list[dict[str, Any]]:
    """Cheap keyword scope: keep records whose text mentions the domain or its close cousins.

    Deliberately loose — the specialist reads the whole slice, and we would rather over-scope
    than have the LLM see nothing. Records with no free-text fields (raw counts) are kept for
    every domain since they may still be relevant.
    """
    keywords = {
        "energy": ("energy", "oil", "gas", "refinery", "pipeline", "opec", "grid", "power"),
        "geopolitics": ("conflict", "military", "sanction", "war", "diplomatic", "border",
                         "geopolitical", "escalation"),
        "macro": ("economy", "inflation", "rates", "central bank", "fed", "ecb", "recession",
                   "gdp", "employment"),
        "disaster": ("disaster", "quake", "flood", "hurricane", "wildfire", "storm",
                      "typhoon", "eruption"),
    }
    needles = keywords[domain]
    out: list[dict[str, Any]] = []
    for rec in evidence:
        blob = json.dumps(rec, default=str).lower()
        if any(k in blob for k in needles) or not any(
                isinstance(v, str) for v in rec.values()):
            out.append(rec)
    return out


def debate(
    evidence: list[dict[str, Any]],
    *,
    as_of: str = "",
    domains: tuple[Domain, ...] = _DOMAINS,
    client: httpx.Client | None = None,
) -> list[FiredThesis]:
    """Run one specialist per domain, then the adversary against each claim.

    Only ``UPHELD`` and ``WEAK`` (demoted) claims survive. A claim with no critique — the
    adversary failed — is dropped conservatively rather than kept unchallenged.
    """
    fired: list[FiredThesis] = []
    adversary = Adversary(client=client)
    for domain in domains:
        slice_ = _domain_slice(evidence, domain)
        if not slice_:
            continue
        claim = SpecialistReader(domain, client=client).read(slice_, as_of=as_of)
        if claim is None or claim.direction == "neutral" or claim.confidence < 0.2:
            continue
        critique = adversary.critique(claim, slice_)
        if critique is None or critique.verdict == "FALSIFIED":
            continue
        fired.append(FiredThesis(
            domain=claim.domain,
            thesis=claim.thesis,
            direction=claim.direction,
            confidence=max(0.0, claim.confidence - critique.demote_by),
            inference=claim.inference,
            evidence=list(claim.evidence),
            adversary_verdict=critique.verdict,
            adversary_reason=critique.reason,
        ))
    return fired
