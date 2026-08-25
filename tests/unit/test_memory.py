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
    assert can_carry_cross_session_memory("messenger") is True
    assert can_carry_cross_session_memory("instagram") is True
    # Legacy combined id (pre-§3.1.2-split sessions) keeps its class for old rows.
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
async def test_widget_summaries_load_only_behind_a_resolved_lead():
    """Round 5C item 19: the widget stays a WEAK channel for session identity
    (§3.4.9 — a widget token alone never inherits history), but a resolved lead
    on a widget turn was proven by an in-conversation identifier, and THAT proof
    is what unlocks the lead's prior summaries — same bar as a strong channel."""
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
        # In-session identity proof (a resolved lead) carries the memory.
        out = await memory.load_cross_session_summaries(
            s, admin_id=admin, instance_id=iid, channel="widget", resolved_lead_id=lead
        )
        assert out == ["prior chat"]
        # The widget token alone (no resolved lead) still carries NOTHING.
        anonymous = await memory.load_cross_session_summaries(
            s, admin_id=admin, instance_id=iid, channel="widget", resolved_lead_id=None
        )
        assert anonymous == []
        # A strong channel (email) for the same lead carries it as before.
        strong = await memory.load_cross_session_summaries(
            s, admin_id=admin, instance_id=iid, channel="email", resolved_lead_id=lead
        )
        assert strong == ["prior chat"]


def test_cross_session_summary_injection_bound_is_ten():
    """§3.4.10 / Decision #38: the default injection cap is N=10, a named platform
    constant — not admin-configurable."""
    import inspect

    assert memory.CROSS_SESSION_SUMMARY_LIMIT == 10
    default = inspect.signature(memory.load_cross_session_summaries).parameters["limit"].default
    assert default == memory.CROSS_SESSION_SUMMARY_LIMIT == 10


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_cross_session_summaries_exclude_older_than_12_months():
    """Summaries older than the rolling 12-month window are excluded from injection
    so the injected context — and the footprint — plateaus (§3.4.10)."""
    from datetime import UTC, datetime, timedelta

    from app.models.conversation import SessionSummary

    admin, iid, lead = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    async with tenant_session(admin) as s:
        # One recent summary (in-window) and one older than 12 months (out-of-window).
        s.add(
            SessionSummary(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                resolved_lead_id=lead,
                session_id=uuid.uuid4(),
                summary="recent",
                created_at=now - timedelta(days=30),
            )
        )
        s.add(
            SessionSummary(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                resolved_lead_id=lead,
                session_id=uuid.uuid4(),
                summary="ancient",
                created_at=now - timedelta(days=400),
            )
        )
        await s.flush()
        out = await memory.load_cross_session_summaries(
            s,
            admin_id=admin,
            instance_id=iid,
            channel="email",
            resolved_lead_id=lead,
            now=now,
        )
        assert out == ["recent"]  # the >12mo summary is excluded
