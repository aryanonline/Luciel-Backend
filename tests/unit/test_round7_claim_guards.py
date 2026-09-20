"""Two deterministic guards that were firing on ordinary sentences (round 7 WP-2, item 2).

* ``classify_request_human`` fired on a bare "human", "real person" or "speak to a":
  "Can I speak to a hygienist?" muted Luciel for the rest of the conversation. A
  request for a person is now an ask-for-a-person PHRASE.
* ``_FIRST_PERSON_SUCCESS_CLAIM`` matched a bare "is now / are now / has been ...
  booked": a paraphrase of the knowledge base ("Appointments are now booked through
  the portal") was rewritten into the unfounded-claim replacement. The subject must
  be first person, or the customer's own thing ("your appointment", "it").
"""

from __future__ import annotations

from app.runtime.escalation import classify_request_human, evaluate_intake
from app.runtime.orchestrator import _FIRST_PERSON_SUCCESS_CLAIM


# --- asking FOR a person vs. mentioning one ------------------------------------------ #
def test_mentioning_a_human_is_not_asking_for_one() -> None:
    for text in (
        "Can I speak to a hygienist?",
        "Do you have human staff?",
        "Is a real person available on weekends?",
    ):
        assert classify_request_human(text) == 0.0, text
        assert evaluate_intake([text]).fired is False, text


def test_asking_for_a_person_still_hands_off() -> None:
    for text in (
        "can I talk to a human?",
        "I want to speak to a real person",
        "I want to speak to a human please",
        "please connect me with someone",
        "let me talk to a real person",
        "human please",
        "a real person please",
        "I'd like a real person",
        "I need an agent",
        "can you get me through to someone?",
        "put me in touch with the owner",
        "delete my data",
    ):
        assert classify_request_human(text) >= 0.85, text


# --- the success-claim guard needs a first-person (or customer-owned) subject ------- #
def test_a_knowledge_paraphrase_with_a_generic_subject_is_not_an_unfounded_claim() -> None:
    for text in (
        "Appointments are now booked through the portal.",
        "Deliveries have been delayed this week because of the weather.",
        "Bookings are now confirmed by email within a day.",
        "Quotes are sent within two business days.",
        "Orders have been shipped from our Toronto warehouse since May.",
    ):
        assert not _FIRST_PERSON_SUCCESS_CLAIM.search(text), text


def test_a_claim_about_the_customers_own_thing_is_still_caught() -> None:
    for text in (
        "I've booked you in for Tuesday at 10.",
        "We have sent the quote to your inbox.",
        "Your appointment has been scheduled.",
        "Your booking is now confirmed and scheduled for Monday.",
        "It's been added to your calendar.",
        "That has been sent to the address on file.",
        "I can confirm it has been sent to your inbox.",
    ):
        assert _FIRST_PERSON_SUCCESS_CLAIM.search(text), text
