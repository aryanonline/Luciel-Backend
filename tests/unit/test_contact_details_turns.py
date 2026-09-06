"""Contact hand-over classifier (2026-09-06 E2E walk, E2E-5).

A visitor answering Luciel's OWN contact ask — "I'm Walk Lead, you can reach me at
walk.lead@example.com" — retrieves nothing, scores 0.0, and the grounding floor
replaced the model's thank-you with the hand-off line a second time. Handing over an
email or a phone number carries no factual claim to ground; a question in the same
breath keeps the floor.
"""

from __future__ import annotations

import pytest
from app.runtime.escalation import (
    Signal,
    evaluate_outcome,
    is_contact_details_turn,
    is_conversational_turn,
)

CONTACT = [
    "I'm Walk Lead, you can reach me at walk.lead@example.com",  # the live-caught turn
    "walk.lead@example.com",
    "Jane Miller, jane.miller@example.com",
    "call me at 604-555-0100",
    "my number is +1 (604) 555-0100",
    "It's Tom. tom.baker@example.com or 6045550100 works",
]

NOT_CONTACT = [
    "What are your hours?",
    "how much is a water heater",
    "I'm Tom, tom@example.com — how much is whitening?",  # a question rides along
    "can you call me at 604-555-0100",  # seeks an action
    "I want a quote, my email is a@b.co",  # intent word keeps the floor
    "my budget is $5000",  # money, no contact detail
    "order 12345",  # too few digits to be a phone number
    "thanks",
    "",
    "   ",
]


@pytest.mark.parametrize("text", CONTACT)
def test_contact_hand_overs_classify(text: str) -> None:
    assert is_contact_details_turn(text) is True, text
    # Digits stay substantive for the SOCIAL classifier; this one stands alone.
    assert is_conversational_turn(text) is False, text


@pytest.mark.parametrize("text", NOT_CONTACT)
def test_questions_and_intent_never_classify_as_contact(text: str) -> None:
    assert is_contact_details_turn(text) is False, text


def test_a_contact_hand_over_is_not_a_cannot_answer_even_at_grounding_zero() -> None:
    decision = evaluate_outcome(
        message="I'm Walk Lead, you can reach me at walk.lead@example.com",
        plan_confidence=1.0,
        grounding_score=0.0,
    )
    assert decision.fired is False


def test_a_question_with_an_email_keeps_the_floor() -> None:
    decision = evaluate_outcome(
        message="I'm Tom, tom@example.com — how much is whitening?",
        plan_confidence=1.0,
        grounding_score=0.0,
    )
    assert decision.fired is True
    assert decision.signal is Signal.cannot_answer


def test_high_value_still_wins_over_the_contact_exemption() -> None:
    # Gate ordering is untouched: opportunity first, then the floor. The budget and
    # the urgency came in earlier turns (F106: scored over the session context).
    decision = evaluate_outcome(
        message="tom@example.com",
        plan_confidence=1.0,
        grounding_score=0.0,
        context="my budget is $5000 and I'd like to start this week",
    )
    assert decision.fired is True
    assert decision.signal is Signal.high_value_lead
