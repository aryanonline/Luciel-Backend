"""Employee-grade judgment, deterministic half (2026-09-05 audit WP5).

* "Are you a human?" asks what Luciel is; it does not ask for a person (F118).
* A hot lead is scored over the conversation, not one message (F106).
* A first-person success claim with no tool behind it is caught (F110).
* Only answer-bearing tools ground a reply (F119).
* The session summary carries the facts the lead volunteered (§3.4.7).
* A bare clarifying question is the one ungrounded draft that may go out (F100).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.models.conversation import Transcript
from app.runtime import memory
from app.runtime.escalation import (
    Signal,
    classify_request_human,
    evaluate_outcome,
    high_value_lead_score,
)
from app.runtime.orchestrator import (
    _FIRST_PERSON_SUCCESS_CLAIM,
    ANSWER_BEARING_TOOLS,
    is_clarifying_question,
)


# --- F118 ----------------------------------------------------------------------- #
def test_asking_what_luciel_is_does_not_hand_the_conversation_off():
    for text in (
        "are you a human?",
        "Is this a bot",
        "am I talking to a real person?",
        "r u an AI",
    ):
        assert classify_request_human(text) == 0.0, text


def test_asking_for_a_person_still_does():
    for text in (
        "can I talk to a human?",
        "I want to speak to a real person",
        "please connect me with someone",
        "delete my data",
    ):
        assert classify_request_human(text) >= 0.85, text


# --- F106 ----------------------------------------------------------------------- #
def test_a_budget_in_one_turn_and_urgency_in_the_next_is_one_hot_lead():
    alone = high_value_lead_score("can we start this week?")
    with_context = high_value_lead_score(
        "can we start this week?", context="hi, my budget is around $5000 for the deck"
    )
    assert alone < 0.5 <= with_context
    decision = evaluate_outcome(
        message="can we start this week?",
        plan_confidence=1.0,
        grounding_score=1.0,
        context="my budget is around $5000",
    )
    assert decision.fired and decision.signal is Signal.high_value_lead


# --- F110 ----------------------------------------------------------------------- #
def test_first_person_claims_are_caught_and_knowledge_statements_are_not():
    caught = (
        "I've booked you in for Tuesday at 10.",
        "Done — we have sent the quote to your inbox.",
        "Your appointment has been scheduled.",
        "It's been added to your calendar.",
        # A modal before a SECOND subject does not launder a completed claim (R6-8).
        "I can confirm it has been sent to your inbox.",
    )
    for text in caught:
        assert _FIRST_PERSON_SUCCESS_CLAIM.search(text), text
    left_alone = (
        "Orders are confirmed by email within a day.",
        "We deliver across the city every weekday.",
        "Bookings can be scheduled up to 30 days ahead.",
        # Round 6 dev walk 2026-09-13 (R6-8): a promise is not a claim. The stanza tells
        # the model the platform delivers a copy, so this is the honest channel-switch
        # wording - the guard used to read "I ... sent" as an action no tool performed.
        "I'll have a copy sent to your email once you confirm the address.",
        "I can send a copy to your email if you share the address.",
        "We will make sure a copy is sent to you.",
        "A copy will be sent to your email.",
    )
    for text in left_alone:
        assert not _FIRST_PERSON_SUCCESS_CLAIM.search(text), text


# --- F119 ----------------------------------------------------------------------- #
def test_only_answer_bearing_tools_ground_a_reply():
    assert {"lookup_record", "check_availability", "book_appointment"} <= ANSWER_BEARING_TOOLS
    for side_effect in (
        "push_to_crm",
        "schedule_callback",
        "send_email",
        "send_sms",
        "bring_your_own_webhook",
    ):
        assert side_effect not in ANSWER_BEARING_TOOLS


# --- F100 ----------------------------------------------------------------------- #
def test_a_bare_clarifying_question_is_sendable_and_an_assertion_is_not():
    assert is_clarifying_question("What's the best email to reach you at?")
    assert is_clarifying_question("Echo: Do you have any openings?")
    assert not is_clarifying_question("We open at 9 on weekdays. Would you like to book?")
    assert not is_clarifying_question("Yes, we carry Rheem and Bradford White heaters.")
    assert not is_clarifying_question("?" * 200)


# --- §3.4.7 summary --------------------------------------------------------------- #
def _turn(role: str, text: str) -> Transcript:
    return Transcript(
        id=uuid.uuid4(),
        admin_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        role=role,
        text=text,
        at=datetime.now(UTC),
    )


def test_the_summary_keeps_the_leads_facts_and_notes_a_person_joined():
    history = [
        _turn("lead", "Hi, I'm looking for a deck quote, budget around $5,000"),
        _turn("luciel", "Happy to help — what's a good email for you?"),
        _turn("lead", "dana@lead.example, ideally this week"),
        _turn("human_agent", "Ana here — I can come by Tuesday."),
    ]
    summary = memory.summarize(history)
    assert summary.startswith("Intent: Hi, I'm looking for a deck quote")
    assert "| Turns: 4" in summary
    # Round 7 WP-3c item 4 (§3.4.11): the address is a presence flag, never a value.
    assert "email on file" in summary and "dana@lead.example" not in summary
    assert "budget $5,000" in summary
    assert "timing this week" in summary
    assert "A person joined this conversation" in summary
    assert len(summary) <= 600


def test_the_summary_never_lifts_a_fact_luciel_stated():
    history = [
        _turn("lead", "how much is a deck?"),
        _turn("luciel", "Decks start at $4,000 — email us at sales@acme.example."),
    ]
    summary = memory.summarize(history)
    assert "| Facts:" not in summary  # nothing Luciel said becomes a lead fact
    # The echoed line is minimized too (round 7 WP-3c item 4, §3.4.11): an address
    # Luciel spoke is as absent from the persisted record as one the lead typed.
    expected = (
        "Intent: how much is a deck? | Last response: Decks start at $4,000 — email us at "
        "email on file. | Turns: 2"
    )
    assert summary == expected
