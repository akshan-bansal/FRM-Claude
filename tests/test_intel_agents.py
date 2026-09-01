"""Tests for intel/agents.py — specialist readers, adversary, and the debate loop.

Mocked at the HTTP transport layer with respx. The module code itself has no test-only branch —
real Anthropic Messages API calls in production, mocked API calls here.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from trading_live_claude.intel.agents import (
    Adversary,
    Claim,
    Critique,
    FiredThesis,
    SpecialistReader,
    debate,
)

_URL = "https://api.anthropic.com/v1/messages"


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test needs an API key present so agents does not short-circuit to None."""
    from trading_live_claude.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")


def _api_response(text: str, status: int = 200) -> httpx.Response:
    """Shape Anthropic's Messages API response around a text payload."""
    return httpx.Response(status, json={
        "id": "msg_test", "type": "message", "role": "assistant",
        "model": "claude-sonnet-5", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
    })


def _claim_json(**overrides: object) -> str:
    payload = {"thesis": "Energy supply shock",
                "direction": "constructive",
                "confidence": 0.7,
                "inference": "Refinery outage in the Persian Gulf reduces distillate supply.",
                "evidence": ["Reuters: outage confirmed", "Bloomberg: FOB spread widening"]}
    payload.update(overrides)
    return json.dumps(payload)


def _critique_json(verdict: str, reason: str, demote_by: float = 0.0) -> str:
    return json.dumps({"verdict": verdict, "reason": reason, "demote_by": demote_by})


# ---- SpecialistReader --------------------------------------------------------


@respx.mock
def test_specialist_reader_parses_a_well_formed_claim() -> None:
    respx.post(_URL).mock(return_value=_api_response(_claim_json()))
    r = SpecialistReader("energy")
    got = r.read([{"id": "EV-1", "title": "Refinery outage", "sources": ["Reuters"]}])
    assert isinstance(got, Claim)
    assert got.domain == "energy"
    assert got.direction == "constructive"
    assert 0.0 <= got.confidence <= 1.0
    assert "outage" in got.inference.lower()
    assert len(got.evidence) == 2


@respx.mock
def test_specialist_reader_clamps_confidence_and_normalizes_direction() -> None:
    """Bad direction → 'neutral'; out-of-range confidence → clamped to [0, 1]."""
    respx.post(_URL).mock(return_value=_api_response(
        _claim_json(direction="bullish", confidence=1.7)))
    got = SpecialistReader("energy").read([{"id": "EV"}])
    assert got is not None
    assert got.direction == "neutral"
    assert got.confidence == 1.0


@respx.mock
def test_specialist_reader_returns_none_on_bad_json() -> None:
    """The LLM produced prose without a JSON object — drop the claim, do not fabricate."""
    respx.post(_URL).mock(return_value=_api_response(
        "Sorry, I need more evidence to make a claim here."))
    assert SpecialistReader("energy").read([{"id": "EV"}]) is None


@respx.mock
def test_specialist_reader_returns_none_on_http_error() -> None:
    """A 500 must not raise or return a stub — return None so the caller drops the claim."""
    respx.post(_URL).mock(return_value=httpx.Response(500, json={"error": "server"}))
    assert SpecialistReader("energy").read([{"id": "EV"}]) is None


def test_specialist_reader_rejects_unknown_domain() -> None:
    """Domain enum is enforced at construction — no free-form domains sneak in."""
    with pytest.raises(ValueError, match="unknown domain"):
        SpecialistReader("cheese")            # type: ignore[arg-type]


@respx.mock
def test_specialist_reader_prompts_include_the_evidence_bundle() -> None:
    """Regression on the prompt shape — the evidence must reach the model."""
    route = respx.post(_URL).mock(return_value=_api_response(_claim_json()))
    SpecialistReader("energy").read([{"id": "EV-42", "title": "Refinery outage"}])
    assert route.call_count == 1
    body = json.loads(route.calls.last.request.content)
    # System prompt names the domain and forbids trade recommendations.
    assert "energy" in body["system"].lower()
    assert "hypothesis" in body["system"].lower() or "posture" in body["system"].lower()
    user = body["messages"][0]["content"]
    assert "EV-42" in user and "Refinery outage" in user


# ---- Adversary ---------------------------------------------------------------


def _claim() -> Claim:
    return Claim(domain="energy", thesis="Supply shock", direction="constructive",
                 confidence=0.7, inference="Outage reduces distillate supply.",
                 evidence=["Reuters: outage confirmed"])


@respx.mock
def test_adversary_normalizes_verdict_and_zeros_demote_on_upheld() -> None:
    """Verdict UPHELD → demote must be 0 regardless of what the model returned."""
    respx.post(_URL).mock(return_value=_api_response(
        _critique_json("UPHELD", "Both cited sources agree.", demote_by=0.3)))
    got = Adversary().critique(_claim(), [{"id": "EV-1", "title": "Refinery outage"}])
    assert isinstance(got, Critique)
    assert got.verdict == "UPHELD"
    assert got.demote_by == 0.0


@respx.mock
def test_adversary_defaults_bad_verdict_to_weak() -> None:
    respx.post(_URL).mock(return_value=_api_response(
        _critique_json("MAYBE", "unclear", demote_by=0.2)))
    got = Adversary().critique(_claim(), [{}])
    assert got is not None and got.verdict == "WEAK"
    assert got.demote_by == 0.2


@respx.mock
def test_adversary_returns_none_on_bad_json() -> None:
    respx.post(_URL).mock(return_value=_api_response("blah blah"))
    assert Adversary().critique(_claim(), [{"a": 1}]) is None


# ---- debate ------------------------------------------------------------------


@respx.mock
def test_debate_upheld_claim_survives_with_full_confidence() -> None:
    """Specialist proposes, adversary UPHOLDS: claim fires, confidence unchanged."""
    calls = iter([
        _api_response(_claim_json()),                              # energy specialist
        _api_response(_critique_json("UPHELD", "Both sources agree.")),  # adversary
    ])
    respx.post(_URL).mock(side_effect=lambda req: next(calls))
    evidence = [{"id": "EV-1", "title": "energy refinery outage", "sources": ["Reuters"]}]
    fired = debate(evidence, domains=("energy",))
    assert len(fired) == 1
    ft: FiredThesis = fired[0]
    assert ft.domain == "energy"
    assert ft.confidence == 0.7            # unchanged
    assert ft.adversary_verdict == "UPHELD"


@respx.mock
def test_debate_falsified_claim_is_dropped() -> None:
    calls = iter([
        _api_response(_claim_json()),
        _api_response(_critique_json("FALSIFIED",
                                      "Bundle contains an OPEC statement denying the outage.")),
    ])
    respx.post(_URL).mock(side_effect=lambda req: next(calls))
    fired = debate(
        [{"id": "EV", "title": "energy outage rumor"}],
        domains=("energy",),
    )
    assert fired == []


@respx.mock
def test_debate_weak_claim_survives_with_demoted_confidence() -> None:
    """WEAK verdicts don't drop the claim but they do lower its confidence."""
    calls = iter([
        _api_response(_claim_json(confidence=0.8)),
        _api_response(_critique_json("WEAK",
                                      "Only one source; corroboration missing.",
                                      demote_by=0.3)),
    ])
    respx.post(_URL).mock(side_effect=lambda req: next(calls))
    fired = debate(
        [{"id": "EV", "title": "energy outage"}],
        domains=("energy",),
    )
    assert len(fired) == 1
    assert abs(fired[0].confidence - 0.5) < 1e-9         # 0.8 - 0.3
    assert fired[0].adversary_verdict == "WEAK"


@respx.mock
def test_debate_neutral_specialist_reads_do_not_reach_the_adversary() -> None:
    """A specialist that returns direction=neutral is dropped BEFORE the adversary is called."""
    route = respx.post(_URL).mock(return_value=_api_response(
        _claim_json(direction="neutral", confidence=0.1)))
    fired = debate(
        [{"id": "EV", "title": "energy activity report"}],
        domains=("energy",),
    )
    # Only ONE HTTP call — the specialist. No adversary call fired.
    assert route.call_count == 1
    assert fired == []


@respx.mock
def test_debate_empty_domain_slice_makes_no_calls_at_all() -> None:
    """No records mention 'macro' keywords → skip the specialist entirely rather than ask blind."""
    route = respx.post(_URL).mock(return_value=_api_response(_claim_json()))
    evidence = [{"id": "EV", "title": "quake in Anatolia", "sources": ["USGS"]}]
    fired = debate(evidence, domains=("macro",))
    assert fired == []
    assert route.call_count == 0
