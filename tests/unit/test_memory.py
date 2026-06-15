"""Conversation memory + summarization + cross-session HARD RULE (Architecture §3.4.6/7/9)."""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.conversation import Transcript
from app.runtime import memory
from app.runtime.sessions import can_carry_cross_session_memory

pytestmark = [pytest.mark.asyncio]


def _t(role: str, text: str) -> Transcript:
    return Transcript(
        id=uuid.uuid4(),
        admin_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        role=role,
        text=text,
    )


def test_summarize_is_transcript_grounded():
    summary = memory.summarize([_t("lead", "I need a quote"), _t("luciel", "Sure!")])
    assert "I need a quote" in summary  # intent echoed from the transcript
    assert "Turns: 2" in summary


def test_summarize_empty_history():
    assert memory.summarize([]) == "No conversation content."


def test_weak_widget_identity_never_carries_cross_session_memory():
    # HARD RULE §3.4.9: weak widget token never inherits history.
    assert can_carry_cross_session_memory("widget") is False
    assert can_carry_cross_session_memory("email") is True
    assert can_carry_cross_session_memory("instagram_messenger") is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_cross_session_summaries_empty_without_resolved_lead():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        out = await memory.load_cross_session_summaries(
            s, admin_id=admin, instance_id=iid, channel="email", resolved_lead_id=None
        )
        assert out == []  # no lead → no history


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_cross_session_summaries_empty_for_weak_widget_channel():
    admin, iid, lead = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        await memory.persist_summary(
            s,
            admin_id=admin,
            instance_id=iid,
            session_id=uuid.uuid4(),
            resolved_lead_id=lead,
            summary="prior chat",
        )
        # Even with a resolved lead, the WIDGET channel cannot carry memory (§3.4.9).
        out = await memory.load_cross_session_summaries(
            s, admin_id=admin, instance_id=iid, channel="widget", resolved_lead_id=lead
        )
        assert out == []
        # A strong channel (email) for the same lead does carry it.
        strong = await memory.load_cross_session_summaries(
            s, admin_id=admin, instance_id=iid, channel="email", resolved_lead_id=lead
        )
        assert strong == ["prior chat"]
