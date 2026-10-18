"""The retrieval skip applies only when the WHOLE message is a pleasantry or a contact
detail (round 7 judgment hardening, review finding "retrieval skip drops the
knowledge search on real requests").

``retrieval_skippable`` trusted two classifiers that read how a message OPENS
(``is_conversational_turn``: starts with hi / ok / yes, or says thanks anywhere) or
what it CONTAINS (``is_contact_details_turn``: an email or a phone number). A real
request phrased as a noun phrase or an imperative — no question mark, no
interrogative — therefore skipped the knowledge search: "Hi, opening hours on
Saturday please" was answered from nothing although the hours were on file.
"""

from __future__ import annotations

import pytest
from app.runtime.escalation import substantive_remainder
from app.runtime.llm_router import should_route_fast
from app.runtime.orchestrator import retrieval_skippable, routing_hints

# Nothing to look up: the whole message is the greeting, the acknowledgement, the
# goodbye or the contact detail (with the customer's own name around it).
SKIP = [
    "thanks!",
    "No its alright thankyou",  # the live-caught social turn (2026-08-18)
    "hi",
    "Hello!",
    "hey there",
    "Good morning",
    "ok thanks",
    "okay great",
    "sure",
    "yes please",
    "sounds good",
    "got it",
    "that's all",
    "no thanks",
    "ok that works for me",
    "thanks for your help",
    "great, see you then",
    "thank you very much, have a lovely weekend",
    "ok bye for now",
    "you can reach me at jane@example.com",
    "walk.lead@example.com",
    "I'm Walk Lead, you can reach me at walk.lead@example.com",  # E2E walk 2026-09-06
    "Jane Miller, jane.miller@example.com",
    "call me at 604-555-0100",
    "my number is +1 (604) 555-0100",
    "It's Tom. tom.baker@example.com or 6045550100 works",
]

# A request rides along: the search must run, whatever the message opens with.
SEARCH = [
    # the review's own sentences — every one of these used to skip the search
    "Hi, opening hours on Saturday please",
    "Hello, I'm interested in teeth whitening",
    "Hi, I'd like to know about your cancellation policy",
    "ok and the address",
    "Thanks! Your prices for implants please",
    "Delivery to Mississauga, thanks",
    "My number is 416-555-0143. Wondering if you service Oakville",
    # a question or an interrogative always searched, and still does
    "thanks, what about parking",
    "my email is a@b.com, do you deliver to Leeds",
    "Thanks — and do you open Saturdays?",
    "jane@example.com — when could you fit me in?",
    # the business's own details asked for as a bare noun phrase
    "ok and the phone number",
    "great, saturday morning",
    "yes the deluxe package",
    "ok, one night please",
    "Hi, emergency care please",
    "text me the address at 416-555-0143",
    # unsure -> retrieve: a name with no introduction might be anything
    "John Smith 416-555-0143",
    # not social, not contact: never skippable in the first place
    "how much is a water heater",
    "I need help with my order",
]


@pytest.mark.parametrize("text", SKIP)
def test_a_whole_message_pleasantry_or_contact_detail_skips_the_search(text: str) -> None:
    assert substantive_remainder(text) == frozenset(), text
    assert retrieval_skippable(text, gather_pending=False) is True, text


@pytest.mark.parametrize("text", SEARCH)
def test_a_request_behind_a_greeting_or_a_contact_detail_keeps_the_search(text: str) -> None:
    assert retrieval_skippable(text, gather_pending=False) is False, text


@pytest.mark.parametrize("text", SKIP)
def test_a_pending_gather_keeps_the_search_whatever_the_message(text: str) -> None:
    assert retrieval_skippable(text, gather_pending=True) is False, text


def test_the_remainder_is_what_the_customer_actually_asked_for() -> None:
    assert substantive_remainder("Hi, opening hours on Saturday please") == {
        "opening",
        "hours",
        "saturday",
    }
    assert substantive_remainder("ok and the address") == {"address"}
    assert substantive_remainder(
        "My number is 416-555-0143. Wondering if you service Oakville"
    ) == {"wondering", "service", "oakville"}
    # The customer's own name and address are not a request.
    assert substantive_remainder("I'm Walk Lead, reach me at walk.lead@example.com") == frozenset()
    assert substantive_remainder("") == frozenset()


def test_a_request_behind_a_greeting_keeps_the_primary_model() -> None:
    """The fast lane is for a turn with nothing to ground and nothing for a tool to
    do; "Hi, opening hours on Saturday please" is neither."""
    tools = [{"tool_id": "book_appointment"}]
    request = routing_hints("Hi, opening hours on Saturday please", "system", [], tools)
    assert request["requires_tools"] is True and request["complexity"] == 1.0
    assert should_route_fast(**request) is False
    social = routing_hints("thanks, that's all!", "system", [], tools)
    assert should_route_fast(**social) is True
