"""Deterministic judgment helpers added by round 7 WP-2 (remainder): the once-per-
session hand-off ack and contact ask (item 1), the distinct-question test behind the
owner re-notification (item 1), and the decline classifier (item 2, §3.3.7 rule 4).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from app.runtime.escalation import (
    is_decline_turn,
    is_new_question,
    is_same_question,
    question_terms,
)
from app.runtime.orchestrator import (
    ASK_ATTEMPT_BOUND,
    CONTACT_ASK_LINE,
    CONTACT_ASK_RESPITE_TURNS,
    DECLINED_INPUT_LINE,
    SHORT_HANDOFF_ACKS,
    also_asked_reason,
    contact_ask_due,
    handoff_ack_sent,
    short_handoff_ack,
    unanswered_questions,
)

FULL_ACK = "I've passed this along to the team — they'll follow up with you shortly."


def _turn(role: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(role=role, text=text)


# --- item 1: a distinct unanswered question ----------------------------------------- #
def test_question_terms_keep_what_the_question_is_about() -> None:
    assert question_terms("do you do implants?") == {"implants"}
    assert question_terms("What about implants") == {"implants"}
    assert question_terms("hello??") == set()


@pytest.mark.parametrize(
    ("later", "earlier", "same"),
    [
        ("implants??", "do you do implants?", True),
        ("what about implants", "do you do implants?", True),
        ("how much are crowns?", "do you do implants?", False),
        ("do you open saturdays?", "do you do implants?", False),
        ("hello??", "do you do implants?", True),  # nothing of its own to ask
        ("is parking free at the clinic", "do you have parking", True),
    ],
)
def test_is_same_question_judges_by_content_not_phrasing(
    later: str, earlier: str, same: bool
) -> None:
    assert is_same_question(later, earlier) is same


def test_is_new_question_is_unlike_every_earlier_uncovered_question() -> None:
    earlier = ["do you do implants?", "how much are crowns?"]
    assert is_new_question("do you open saturdays?", earlier) is True
    assert is_new_question("implants??", earlier) is False
    assert is_new_question("what do crowns cost", earlier) is False
    assert is_new_question("??", earlier) is False
    # With nothing uncovered yet, any real question is new.
    assert is_new_question("do you do implants?", []) is True


def test_also_asked_reason_is_the_question_itself_capped() -> None:
    assert also_asked_reason("  how much   are crowns? ") == 'also asked: "how much are crowns?"'
    long = "x" * 300
    reason = also_asked_reason(long)
    assert reason.startswith('also asked: "') and reason.endswith('…"')
    assert len(reason) < 140


# --- item 1: one full ack, one contact ask, per session ------------------------------- #
def test_the_full_ack_is_recognised_in_either_wording() -> None:
    assert handoff_ack_sent([_turn("lead", "hi"), _turn("luciel", FULL_ACK)]) is True
    assert (
        handoff_ack_sent(
            [
                _turn(
                    "luciel",
                    "Of course — I've flagged this for the team. They're away right "
                    "now, so someone will follow up with you tomorrow at 9:00 am (EDT).",
                )
            ]
        )
        is True
    )
    assert handoff_ack_sent([_turn("luciel", "Echo: hi"), _turn("lead", FULL_ACK)]) is False


def test_short_acks_cycle_so_two_in_a_row_never_match() -> None:
    history: list[SimpleNamespace] = [_turn("luciel", FULL_ACK)]
    seen: list[str] = []
    for _ in range(len(SHORT_HANDOFF_ACKS) + 1):
        ack = short_handoff_ack(history)
        seen.append(ack)
        history.append(_turn("luciel", ack))
    assert seen[: len(SHORT_HANDOFF_ACKS)] == list(SHORT_HANDOFF_ACKS)
    assert all(a != b for a, b in zip(seen, seen[1:], strict=False))
    # None of them promises a time or a real-time person, or asks for contact details.
    for ack in SHORT_HANDOFF_ACKS:
        assert "shortly" not in ack and "right now" not in ack and "?" not in ack


def test_contact_ask_goes_out_once_then_after_a_respite_and_never_past_the_bound() -> None:
    assert contact_ask_due([], anonymous=True) is True
    assert contact_ask_due([], anonymous=False) is False
    asked = [_turn("lead", "q1"), _turn("luciel", f"{FULL_ACK}\n\n{CONTACT_ASK_LINE}")]
    # The very next escalating turns do not nag.
    assert contact_ask_due(asked, anonymous=True) is False
    history = list(asked)
    for i in range(CONTACT_ASK_RESPITE_TURNS - 1):
        history += [_turn("lead", f"q{i + 2}"), _turn("luciel", f"ack {i + 2}")]
        assert contact_ask_due(history, anonymous=True) is False
    history += [_turn("lead", "another"), _turn("luciel", "ack")]
    # The respite has passed with still nothing on file: one more ask is allowed…
    assert contact_ask_due(history, anonymous=True) is True
    history += [_turn("lead", "q"), _turn("luciel", f"short\n\n{CONTACT_ASK_LINE}")]
    history += [_turn("lead", f"q{n}") if n % 2 else _turn("luciel", "a") for n in range(12)]
    # …and that was the last: code-composed re-asks are bounded like the model's.
    assert ASK_ATTEMPT_BOUND == 2
    assert contact_ask_due(history, anonymous=True) is False
    # A visitor who is no longer anonymous is never asked.
    assert contact_ask_due(asked, anonymous=False) is False


def test_the_contact_ask_is_never_due_once_the_customer_declined_a_detail() -> None:
    """Item 2 (§3.3.7 rule 4): Luciel said "I won't ask for that again"; the contact
    ask names the very identifiers a booking gather asks for, so it stays off for the
    rest of the session — even where the respite would otherwise allow it."""
    declined = [
        _turn("lead", "book me for 2pm"),
        _turn("luciel", "What email should I send the confirmation to?"),
        _turn("lead", "I'd rather not say"),
        _turn("luciel", f"{DECLINED_INPUT_LINE} I can't complete the booking without it."),
    ]
    assert contact_ask_due(declined, anonymous=True) is False
    history = list(declined)
    for n in range(2 * CONTACT_ASK_RESPITE_TURNS + 2):
        history += [_turn("lead", f"q{n}"), _turn("luciel", f"ack {n}")]
        assert contact_ask_due(history, anonymous=True) is False


def test_unanswered_questions_are_the_lead_turns_a_hand_off_ack_answered() -> None:
    history = [
        _turn("lead", "hi"),
        _turn("luciel", "Echo: hi"),
        _turn("lead", "do you do implants?"),
        _turn("luciel", f"{FULL_ACK}\n\n{CONTACT_ASK_LINE}"),
        _turn("lead", "how much are crowns?"),
        _turn("luciel", SHORT_HANDOFF_ACKS[0]),
        _turn("lead", "do you open saturdays?"),
    ]
    assert unanswered_questions(history) == ["do you do implants?", "how much are crowns?"]


# --- item 2: declining to give a required input (§3.3.7 rule 4) ----------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "I'd rather not",
        "I'd rather not say",
        "rather not, thanks",
        "I would prefer not to",
        "I don't want to give my email",
        "don't want to give you my number",
        "I can't share my number",
        "I won't say",
        "not comfortable sharing my number",
        "I'm not comfortable giving that out",
        "skip that",
        "let's skip it",
        "can we skip the email",
        "never mind",
        "forget it",
        "can you book it without my email?",
        "book me without giving you my number",
        "keep that private",
        "none of your business",
        "no thanks",
        "No",
        "nope.",
        "I'm not giving that out",
        "not going to give you that",
        "I'd rather not, just book it",
    ],
)
def test_is_decline_turn_positives(text: str) -> None:
    assert is_decline_turn(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "I don't want the 3pm slot",  # declines an OPTION, not the detail
        "I'd rather not do mornings",
        "I'd rather have the afternoon",
        "no thanks, 2pm works",  # a choice
        "no, the 10am one",
        "it's not that I don't want to share, here: jane@example.com",  # supplies it
        "my number is 555 123 4567",
        "sure, it's jane@example.com",
        "I don't mind sharing my email",  # negated decline
        "I don't want to wait that long",
        "not comfortable with mornings",
        "can't say I've been before",  # an idiom
        "what time do you open?",
        "leave it for tomorrow?",
        "without a doubt I'll book",
        "",
    ],
)
def test_is_decline_turn_negatives(text: str) -> None:
    assert is_decline_turn(text) is False
