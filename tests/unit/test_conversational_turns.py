"""Conversational-turn classifier (live-caught 2026-08-18).

A caller answering "No its alright thankyou" to Luciel's own follow-up offer tripped
the cannot_answer grounding gate and emailed the owner: a pleasantry retrieves
nothing, scores 0.0, and with live providers pinning confidence at 1.0 the grounding
floor alone decided. A social turn contains no factual claim to ground, so the floor
must not apply to it — while REAL questions keep the full OR-gate (doctrine: it must
never become an AND, and this classifier must never misread a question as social).
"""

from __future__ import annotations

import pytest
from app.runtime.escalation import is_conversational_turn

CONVERSATIONAL = [
    "No its alright thankyou",  # the live-caught utterance, verbatim
    "no it's alright, thank you",
    "hi",
    "Hello!",
    "hey there",
    "Good morning",
    "thanks",
    "Thank you so much",
    "thankyou",
    "no thanks",
    "No thank you",
    "nothing else",
    "that's all",
    "ok",
    "okay great",
    "sounds good",
    "got it",
    "perfect",
    "sure",
    "alright",
    "yes please",
    "yep",
    "bye",
    "goodbye",
    "have a great day",
    "take care",
    "you're welcome",
    "no worries",
]

NOT_CONVERSATIONAL = [
    "What's your return policy for electronics?",  # pinned regression texts
    "Do you offer a warranty?",
    "Some question",
    "how much is a water heater",  # question shape without the question mark
    "I need help with my order",
    "can you book me in for tuesday",
    "ok but what time do you open",  # ack followed by a real question
    "thanks, and how much would that cost",  # gratitude wrapping a question
    "my budget is $5000",  # money = high-value content, never social
    "call me at 604-555-0100",  # digits = substantive content
    "yes I want to schedule an appointment",  # affirmation carrying intent
    "The heater in my rental unit has been broken for two weeks and the landlord "
    "will not respond to any of my messages, please help",  # long substantive text
    "",
    "   ",
]


@pytest.mark.parametrize("text", CONVERSATIONAL)
def test_social_turns_classify_conversational(text: str) -> None:
    assert is_conversational_turn(text) is True, text


@pytest.mark.parametrize("text", NOT_CONVERSATIONAL)
def test_substantive_turns_never_classify_conversational(text: str) -> None:
    assert is_conversational_turn(text) is False, text
