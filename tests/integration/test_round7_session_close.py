"""Session close as an employee would leave a desk (round 7 WP-2, items 2 and 9).

* A take-over sealed by the session end returns the row to ai mode and records how
  long the person held it; a hand-back records the same measure (item 9).
* A session that closes while Luciel was still waiting for a required input tells the
  owner a booking stalled, naming the field still missing (item 2, §3.3.7 rule 4).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.conversation import EscalationEvent, Transcript
from app.models.conversation import Session as ConvSession
from app.models.instance import Instance, LucielState
from app.models.trace import ToolTraceEvent
from app.runtime import handoff
from app.runtime.session_close import finalize_due_sessions, finalize_session
from sqlalchemy import select


async def _seed(
    admin: uuid.UUID,
    iid: uuid.UUID,
    *,
    channel: str = "widget",
    ask_attempts: int = 0,
    missing_fields: list[str] | None = None,
) -> uuid.UUID:
    sid = uuid.uuid4()
    now = datetime.now(UTC)
    async with tenant_session(admin) as s:
        s.add(Instance(id=iid, admin_id=admin, name="Acme Patios", state=LucielState.active))
        s.add(
            ConvSession(
                id=sid,
                admin_id=admin,
                instance_id=iid,
                channel=channel,
                mode="ai",
                started_at=now - timedelta(minutes=5),
                last_message_at=now,
                input_ask_attempts=ask_attempts,
            )
        )
        await s.flush()
        if missing_fields is not None:
            # The broker's durable trace of the stalled dispatch (§3.3.4 rule 3).
            s.add(
                ToolTraceEvent(
                    admin_id=admin,
                    instance_id=iid,
                    session_id=sid,
                    tool_id="book_appointment",
                    outcome="missing_input",
                    connection_type="calendar",
                    missing_fields=list(missing_fields),
                    at=now,
                )
            )
            await s.flush()
        s.add(
            Transcript(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                session_id=sid,
                role="lead",
                text="can someone call me?",
                at=now,
            )
        )
        await s.flush()
    return sid


async def _events(admin: uuid.UUID, event_type: str) -> list[AdminAuditLog]:
    async with tenant_session(admin) as s:
        return list(
            (
                await s.execute(
                    select(AdminAuditLog)
                    .where(AdminAuditLog.admin_id == admin, AdminAuditLog.event_type == event_type)
                    .order_by(AdminAuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )


async def _session(admin: uuid.UUID, sid: uuid.UUID) -> ConvSession:
    async with tenant_session(admin) as s:
        return (await s.execute(select(ConvSession).where(ConvSession.id == sid))).scalar_one()


# --- item 9: a sealed take-over ends in ai mode, with its duration ------------------ #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_takeover_sealed_by_the_session_end_returns_to_ai_mode_with_its_duration():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    sid = await _seed(admin, iid)
    async with tenant_session(admin) as s:
        await handoff.take_over(
            s, admin_id=admin, instance_id=iid, session_id=sid, actor_user_id=admin
        )
    assert (await _session(admin, sid)).mode == "human_controlled"

    # The widget close beacon two hours later: the person never handed back.
    ended_at = datetime.now(UTC) + timedelta(hours=2)
    async with tenant_session(admin) as s:
        assert await finalize_session(
            s, admin_id=admin, instance_id=iid, channel="widget", session_id=sid, now=ended_at
        )

    sealed = await _session(admin, sid)
    assert sealed.ended_at is not None
    # A stale human_controlled never outlives the session.
    assert sealed.mode == "ai"
    (ended,) = await _events(admin, "human_takeover_ended")
    assert ended.event_payload["trigger"] == "session_timeout"
    # Measured from the persisted take-over start, not from the session's open.
    assert 7195 <= ended.event_payload["duration_seconds"] <= 7205


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_inactivity_sweep_seals_a_takeover_the_same_way():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    sid = await _seed(admin, iid, channel="sms")
    async with tenant_session(admin) as s:
        await handoff.take_over(
            s, admin_id=admin, instance_id=iid, session_id=sid, actor_user_id=admin
        )
    async with tenant_session(admin) as s:
        # sms window is 4h: the boundary (last_message_at + 4h) is the honest end.
        assert (
            await finalize_due_sessions(
                s, admin_id=admin, now=datetime.now(UTC) + timedelta(hours=9)
            )
            == 1
        )
    sealed = await _session(admin, sid)
    assert sealed.mode == "ai" and sealed.ended_at is not None
    (ended,) = await _events(admin, "human_takeover_ended")
    assert 14395 <= ended.event_payload["duration_seconds"] <= 14405


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_hand_back_records_how_long_the_person_held_the_session():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    sid = await _seed(admin, iid)
    async with tenant_session(admin) as s:
        await handoff.take_over(
            s, admin_id=admin, instance_id=iid, session_id=sid, actor_user_id=admin
        )
        await handoff.hand_back(
            s, admin_id=admin, instance_id=iid, session_id=sid, actor_user_id=admin
        )
    (ended,) = await _events(admin, "human_takeover_ended")
    assert ended.event_payload["trigger"] == "hand_back"
    assert 0 <= ended.event_payload["duration_seconds"] <= 5
    assert (await _session(admin, sid)).mode == "ai"


# --- item 2: a session that closes mid-gather tells the owner what stalled ---------- #
async def _escalations(admin: uuid.UUID, sid: uuid.UUID) -> list[EscalationEvent]:
    async with tenant_session(admin) as s:
        return list(
            (await s.execute(select(EscalationEvent).where(EscalationEvent.session_id == sid)))
            .scalars()
            .all()
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_session_closing_mid_gather_tells_the_owner_what_was_still_missing():
    """The broker refused book_appointment for want of an email, Luciel asked, and the
    visitor closed the widget. Nothing ever told the owner a booking stalled; now the
    close writes cannot_answer naming the field, read from the durable tool trace."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    sid = await _seed(admin, iid, ask_attempts=1, missing_fields=["email"])
    async with tenant_session(admin) as s:
        assert await finalize_session(
            s, admin_id=admin, instance_id=iid, channel="widget", session_id=sid
        )
    (row,) = await _escalations(admin, sid)
    assert row.signal == "cannot_answer" and row.gate == "outcome"
    assert list(row.reasons or []) == [
        "still missing email",
        "the conversation ended before the booking could be completed",
    ]
    (fired,) = await _events(admin, "escalation_fired")
    assert fired.event_payload["signal"] == "cannot_answer"
    assert (await _session(admin, sid)).ended_at is not None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_sweep_reports_a_prose_stall_as_an_unanswered_question():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    sid = await _seed(admin, iid, channel="sms", ask_attempts=1)  # no tool trace behind it
    async with tenant_session(admin) as s:
        assert (
            await finalize_due_sessions(
                s, admin_id=admin, now=datetime.now(UTC) + timedelta(hours=9)
            )
            == 1
        )
    (row,) = await _escalations(admin, sid)
    assert row.signal == "cannot_answer"
    assert list(row.reasons or [])[0] == "the customer never answered Luciel's last question"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_session_with_no_pending_gather_closes_without_an_escalation():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    sid = await _seed(admin, iid, ask_attempts=0, missing_fields=["email"])  # resolved later
    async with tenant_session(admin) as s:
        assert await finalize_session(
            s, admin_id=admin, instance_id=iid, channel="widget", session_id=sid
        )
    assert await _escalations(admin, sid) == []
