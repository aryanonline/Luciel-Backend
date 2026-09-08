"""Judgment quality, round 6 WP-G (2026-09-08 audit F102 + F124).

* The owner's own hot-lead phrases are the one admin input to the fixed
  high_value_lead signal — they add weight, never a signal (F102).
* Live providers report a REAL self-reported confidence through a trailing tag the
  prompt asks for; absent → 1.0 exactly as before (F124).
* The complaint lexicon is wide and negation-aware; a polite refund or cancellation
  request is a request, not anger (F124).
* Every fired decision says why, in the owner's words (F124).
"""

from __future__ import annotations

import pytest
from app.runtime import personality
from app.runtime.escalation import (
    Signal,
    evaluate_intake,
    evaluate_outcome,
    high_value_lead_reasons,
    negative_terms,
    sentiment_score,
)
from app.runtime.input_safety import build_system_prompt
from app.runtime.llm_router import LLMResponse
from app.runtime.providers import AnthropicProvider, OpenAIProvider, parse_confidence_tag
from app.schemas_api.luciel import UpdatePersonalityRequest
from pydantic import ValidationError

from tests.unit.test_providers import (
    _Block,
    _FakeAnthropicClient,
    _FakeOpenAIClient,
    _FakeOpenAIResponse,
)


# --- F102: owner phrases ------------------------------------------------------------ #
def test_owner_phrases_match_case_and_whitespace_insensitively():
    p = {"highValueSignals": ["wedding season", "Fleet of trucks"]}
    assert personality.custom_rules_hit(p, "we have a  FLEET of\ntrucks to service") == (
        "Fleet of trucks"
    )
    assert personality.custom_rules_hit(p, "hello there") is None
    assert personality.custom_rules_hit({}, "fleet of trucks") is None
    assert personality.custom_rules_hit(None, "") is None


def test_an_owner_phrase_lifts_a_lukewarm_lead_over_the_line_and_is_named():
    text = "we'd like this done this week"  # time pressure alone: 0.3, below the line
    assert evaluate_outcome(message=text, plan_confidence=1.0, grounding_score=1.0).fired is False
    d = evaluate_outcome(
        message=text, plan_confidence=1.0, grounding_score=1.0, custom_rules_hit="fleet of trucks"
    )
    assert d.fired and d.signal is Signal.high_value_lead
    assert d.reasons == ("wants it soon", 'said "fleet of trucks"')
    score, reasons = high_value_lead_reasons("budget is $5,000, can we book today?")
    assert score == 1.0
    assert reasons == ("named a budget", "wants it soon", "ready to buy or book")


def test_the_phrase_list_is_cleaned_and_bounded():
    req = UpdatePersonalityRequest.model_validate(
        {
            "preset": "warm_concierge",
            "highValueSignals": ["  wedding  season ", "Wedding season", "", "fleet"],
        }
    )
    assert req.high_value_signals == ["wedding season", "fleet"]
    with pytest.raises(ValidationError):
        UpdatePersonalityRequest.model_validate(
            {"preset": "warm_concierge", "highValueSignals": ["x" * 61]}
        )
    with pytest.raises(ValidationError):
        UpdatePersonalityRequest.model_validate(
            {"preset": "warm_concierge", "highValueSignals": [f"phrase {i}" for i in range(21)]}
        )
    # Still no model field, ever (Arch §3.4.3).
    with pytest.raises(ValidationError):
        UpdatePersonalityRequest.model_validate({"preset": "warm_concierge", "model": "x"})


# --- F124: a real confidence ------------------------------------------------------- #
def test_the_trailing_confidence_tag_is_parsed_stripped_and_clamped():
    assert parse_confidence_tag("We open at 9.\n<confidence>0.35</confidence>") == (
        "We open at 9.",
        0.35,
    )
    assert parse_confidence_tag("We open at 9.") == ("We open at 9.", 1.0)
    assert parse_confidence_tag("x <CONFIDENCE> 1 </CONFIDENCE>\n") == ("x", 1.0)
    assert parse_confidence_tag("x <confidence>7</confidence>") == ("x", 1.0)
    quoted = "The tag <confidence>0.2</confidence> is part of the answer."
    assert parse_confidence_tag(quoted) == (quoted, 1.0)
    assert parse_confidence_tag("") == ("", 1.0)


async def test_both_live_providers_report_the_tag_and_never_show_it():
    anthropic = AnthropicProvider(api_key="fake-anthropic-key", model="model-x")
    anthropic._client = _FakeAnthropicClient(
        response=_Block(
            content=[_Block(type="text", text="Closed Sundays.\n<confidence>0.4</confidence>")]
        )
    )
    out = await anthropic.complete("sys", [{"role": "user", "content": "hi"}])
    assert (out.text, out.confidence) == ("Closed Sundays.", 0.4)

    openai_p = OpenAIProvider(api_key="fake-openai-key", model="model-y")
    openai_p._client = _FakeOpenAIClient(
        response=_FakeOpenAIResponse("Open daily.<confidence>0.9</confidence>")
    )
    out2 = await openai_p.complete("sys", [{"role": "user", "content": "hi"}])
    assert (out2.text, out2.confidence) == ("Open daily.", 0.9)
    assert isinstance(out2, LLMResponse)


def test_a_low_self_reported_confidence_escalates_even_when_grounded():
    d = evaluate_outcome(message="What are your hours?", plan_confidence=0.3, grounding_score=0.9)
    assert d.fired and d.signal is Signal.cannot_answer
    assert d.reasons == ("the model was 30% sure",)
    d2 = evaluate_outcome(message="What are your hours?", plan_confidence=1.0, grounding_score=0.2)
    assert d2.reasons == ("answer only 20% backed by your knowledge",)


def test_the_prompt_asks_for_the_tag_exactly_once_after_the_style_rules():
    prompt = build_system_prompt("A bakery in Kelowna.")
    assert prompt.count("<confidence>X</confidence>") == 1
    assert prompt.index("Confidence (platform rule)") < prompt.index("A bakery in Kelowna.")


# --- F124: sentiment ----------------------------------------------------------------- #
def test_negation_is_not_a_complaint():
    assert negative_terms("not bad at all, never disappointed") == []
    assert negative_terms("this is terrible and the service was awful") == ["terrible", "awful"]
    assert sentiment_score("it isn't terrible") == 0.0
    assert sentiment_score("absolutely horrendous, a scam") <= -0.7
    assert sentiment_score("This is a nightmare, you people are incompetent.") <= -0.7


def test_a_polite_cancellation_or_refund_request_is_not_anger():
    assert negative_terms("I'd like to cancel my appointment and get a refund please") == []
    assert negative_terms("can I leave a review?") == []
    assert evaluate_intake(["I want a refund", "and cancel the rest", "thanks"]).fired is False


def test_intake_reasons_name_the_words():
    d = evaluate_intake(["this is a scam", "ok", "you people are useless"])
    assert d.fired and d.signal is Signal.strong_negative_sentiment
    assert d.reasons == ("upset in 2 of the last 3 messages", "words: scam, useless")
    h = evaluate_intake(["let me talk to a real person"])
    assert h.reasons == ("asked for a person",)
