"""Round 5C cognition pins — prose ask-bound (18), widget memory (19), and the
delete-my-data hand-off (8)."""

from __future__ import annotations

import pytest
from app.runtime.escalation import classify_request_human, is_repeated_question
from app.runtime.sessions import can_carry_cross_session_memory


def test_repeated_question_matches_the_same_ask_in_fresh_clothes():
    assert is_repeated_question(
        "Sorry — which day works best for you?",
        "Which day works best for you?",
    )


def test_distinct_gather_questions_never_trip_the_bound():
    """A healthy gather asks different things in sequence (§3.3.7 allows asking;
    it prohibits RE-asking)."""
    assert not is_repeated_question(
        "And what time of day suits you?",
        "Which day works best for you?",
    )


def test_non_questions_and_first_questions_are_never_stalls():
    assert not is_repeated_question("Great, you're booked for Tuesday.", "Which day works?")
    assert not is_repeated_question("Which day works for you?", None)
    assert not is_repeated_question("Which day works for you?", "Happy to help with that.")


def test_delete_my_data_requests_hand_off_to_a_human():
    """Round 5C item 8: erasure is the tenant's dashboard action — Luciel must
    hand the request off, never answer around it."""
    assert classify_request_human("please delete my data") > 0
    assert classify_request_human("can you erase my information?") > 0
    assert classify_request_human("forget me and remove my number") > 0
    # Service requests that merely share a verb stay with Luciel.
    assert classify_request_human("can you remove the stain protection add-on?") == 0
    assert classify_request_human("delete that appointment for me") == 0


def test_widget_memory_gate_is_channel_scoped_not_blanket():
    """Round 5C item 19: the widget stays a WEAK channel for identity (sessions
    resume by id alone) — memory.load allows it only because a resolved lead on
    the widget was proven by an in-conversation identifier. The channel-strength
    map itself is unchanged."""
    assert can_carry_cross_session_memory("sms")
    assert not can_carry_cross_session_memory("widget")


@pytest.mark.asyncio
async def test_widget_lead_with_in_session_identity_gets_prior_summaries():
    import uuid
    from unittest.mock import AsyncMock, MagicMock

    from app.runtime import memory

    lead_id = uuid.uuid4()
    summary_row = MagicMock()
    summary_row.summary = "Intent: quote | Last response: sure | Turns: 3"
    result = MagicMock()
    result.scalars.return_value.all.return_value = [summary_row]
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    out = await memory.load_cross_session_summaries(
        session,
        admin_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        channel="widget",
        resolved_lead_id=lead_id,
    )
    assert out == ["Intent: quote | Last response: sure | Turns: 3"]

    # No resolved lead → nothing, exactly as before.
    out = await memory.load_cross_session_summaries(
        session,
        admin_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        channel="widget",
        resolved_lead_id=None,
    )
    assert out == []
