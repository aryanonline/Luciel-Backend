"""The takeover audit trail has an end for every start (audit F078) and the documented
payload (Architecture §3.4.12: trigger + channel + hashed lead, never raw PII).

A takeover the admin never handed back used to end silently when the inactivity
sweep sealed the session: ``human_takeover_started`` with no ``human_takeover_ended``,
so the escalation trail could not say how long a person held a conversation or that
it ended at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.audit import log as audit
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.conversation import Session as ConvSession
from app.models.conversation import Transcript
from app.models.instance import Instance, LucielState
from app.runtime import handoff
from app.runtime.budget import count_session_once
from app.runtime.session_close import finalize_due_sessions
from sqlalchemy import select

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


async def _seed_instance(admin: uuid.UUID, iid: uuid.UUID) -> None:
    async with tenant_session(admin) as s:
        s.add(Instance(id=iid, admin_id=admin, name="Acme Patios", state=LucielState.active))
        await s.flush()


async def _seed_session(
    admin: uuid.UUID, iid: uuid.UUID, *, mode: str, idle: timedelta, lead_id: uuid.UUID | None
) -> uuid.UUID:
    sid = uuid.uuid4()
    last = NOW - idle
    async with tenant_session(admin) as s:
        s.add(
            ConvSession(
                id=sid,
                admin_id=admin,
                instance_id=iid,
                channel="sms",
                mode=mode,
                resolved_lead_id=lead_id,
                started_at=last - timedelta(minutes=5),
                last_message_at=last,
            )
        )
        await s.flush()
        # A conversation a person held was billed like any other: counted through the
        # real fire-once ledger, never by seeding the deprecated ``counted`` column.
        assert await count_session_once(s, admin_id=admin, instance_id=iid, session_id=sid)
        for i, (role, text) in enumerate(
            [("lead", "can someone call me?"), ("human_agent", "Ana here — calling now.")]
        ):
            s.add(
                Transcript(
                    id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    session_id=sid,
                    role=role,
                    text=text,
                    at=last - timedelta(minutes=2 - i),
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


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_takeover_sealed_by_the_inactivity_sweep_records_its_end():
    admin, iid, lead_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_instance(admin, iid)
    held = await _seed_session(
        admin, iid, mode="human_controlled", idle=timedelta(days=2), lead_id=lead_id
    )
    plain = await _seed_session(admin, iid, mode="ai", idle=timedelta(days=2), lead_id=None)

    async with tenant_session(admin) as s:
        sealed = await finalize_due_sessions(s, admin_id=admin, now=NOW)
    assert sealed == 2

    ended = await _events(admin, "human_takeover_ended")
    assert [e.event_payload["session_id"] for e in ended] == [str(held)]
    payload = ended[0].event_payload
    assert payload["trigger"] == "session_timeout"
    assert payload["channel"] == "sms"
    assert payload["resolved_lead_id_hash"] == audit.hash_lead_id(lead_id)
    assert str(lead_id) not in str(payload)  # hashed, never the raw id (§3.4.11)
    assert str(plain) not in str(payload)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_sweep_is_idempotent_on_the_takeover_end_event():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_instance(admin, iid)
    await _seed_session(admin, iid, mode="human_controlled", idle=timedelta(days=2), lead_id=None)

    for _ in range(2):
        async with tenant_session(admin) as s:
            await finalize_due_sessions(s, admin_id=admin, now=NOW)

    assert len(await _events(admin, "human_takeover_ended")) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_take_over_and_hand_back_carry_trigger_channel_and_hashed_lead():
    admin, iid, lead_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_instance(admin, iid)
    sid = await _seed_session(admin, iid, mode="ai", idle=timedelta(minutes=1), lead_id=lead_id)

    async with tenant_session(admin) as s:
        await handoff.take_over(
            s, admin_id=admin, instance_id=iid, session_id=sid, actor_user_id=admin
        )
        await handoff.hand_back(
            s, admin_id=admin, instance_id=iid, session_id=sid, actor_user_id=admin
        )

    (started,) = await _events(admin, "human_takeover_started")
    # Round 7 WP-3b item 8: the trigger says WHO decided — the owner's button here.
    assert started.event_payload["trigger"] == handoff.TRIGGER_ADMIN_INITIATED == "admin_initiated"
    assert started.event_payload["channel"] == "sms"
    assert started.event_payload["resolved_lead_id_hash"] == audit.hash_lead_id(lead_id)
    assert str(lead_id) not in str(started.event_payload)

    (ended,) = await _events(admin, "human_takeover_ended")
    assert ended.event_payload["trigger"] == "hand_back"
    assert ended.event_payload["channel"] == "sms"
    assert ended.event_payload["resolved_lead_id_hash"] == audit.hash_lead_id(lead_id)
    # How long a person held it — a real number off the persisted start, not absent.
    assert isinstance(ended.event_payload["duration_seconds"], int)
    assert ended.event_payload["duration_seconds"] >= 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_luciel_escalation_is_recorded_as_luciel_escalated():
    """Luciel's own judgment (explicit_human_request, a moderation category) is a
    different decision-maker from the owner's button, and the trail must say which."""
    admin, iid, lead_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_instance(admin, iid)
    sid = await _seed_session(admin, iid, mode="ai", idle=timedelta(minutes=1), lead_id=lead_id)

    async with tenant_session(admin) as s:
        await handoff.take_over(
            s,
            admin_id=admin,
            instance_id=iid,
            session_id=sid,
            actor_user_id=None,
            luciel_initiated=True,
        )

    (started,) = await _events(admin, "human_takeover_started")
    assert handoff.TRIGGER_LUCIEL_ESCALATED == "luciel_escalated"
    assert started.event_payload["trigger"] == handoff.TRIGGER_LUCIEL_ESCALATED
    assert started.event_payload["luciel_initiated"] is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_hand_back_measures_the_duration_from_the_persisted_takeover_start():
    """The number is read off ``sessions.human_controlled_since`` (migration 0066), so
    a takeover that began an hour ago hands back with ~3600, not with whatever the
    audit rows happen to say."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_instance(admin, iid)
    sid = await _seed_session(admin, iid, mode="ai", idle=timedelta(minutes=1), lead_id=None)

    async with tenant_session(admin) as s:
        sess = await handoff.take_over(
            s, admin_id=admin, instance_id=iid, session_id=sid, actor_user_id=admin
        )
        assert sess.human_controlled_since is not None
        # Re-asserting the takeover keeps the ORIGINAL start (idempotent, §3.4.12).
        first_start = sess.human_controlled_since
        sess = await handoff.take_over(
            s, admin_id=admin, instance_id=iid, session_id=sid, actor_user_id=admin
        )
        assert sess.human_controlled_since == first_start
        sess.human_controlled_since = datetime.now(UTC) - timedelta(hours=1)
        await s.flush()
        sess = await handoff.hand_back(
            s, admin_id=admin, instance_id=iid, session_id=sid, actor_user_id=admin
        )
        assert sess.human_controlled_since is None  # cleared with the takeover

    (ended,) = await _events(admin, "human_takeover_ended")
    assert 3600 <= ended.event_payload["duration_seconds"] <= 3600 + 60


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_takeover_that_predates_the_start_column_hands_back_with_unknown_duration():
    """A session already human_controlled before 0066 has no recorded start: the
    end event says ``None`` rather than fabricating 0 from a stamp taken now."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_instance(admin, iid)
    sid = await _seed_session(
        admin, iid, mode="human_controlled", idle=timedelta(minutes=1), lead_id=None
    )

    async with tenant_session(admin) as s:
        # The owner presses "Take over" again on the legacy row: still no start.
        sess = await handoff.take_over(
            s, admin_id=admin, instance_id=iid, session_id=sid, actor_user_id=admin
        )
        assert sess.human_controlled_since is None
        await handoff.hand_back(
            s, admin_id=admin, instance_id=iid, session_id=sid, actor_user_id=admin
        )

    (ended,) = await _events(admin, "human_takeover_ended")
    assert ended.event_payload["duration_seconds"] is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_take_over_refuses_an_ended_session():
    """Round 7 WP-3b item 4: an ended session (``ended_at`` set) is never resumed and
    nothing on the customer's transport is waiting on it — switching it to
    human_controlled would show a person "holding" a conversation nobody can answer
    into. Refused with a typed error; no mode change, no budget count, no audit row."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    await _seed_instance(admin, iid)
    sid = await _seed_session(admin, iid, mode="ai", idle=timedelta(minutes=1), lead_id=None)
    async with tenant_session(admin) as s:
        sess = (await s.execute(select(ConvSession).where(ConvSession.id == sid))).scalar_one()
        sess.ended_at = NOW
        await s.flush()

    async with tenant_session(admin) as s:
        with pytest.raises(handoff.SessionEnded) as excinfo:
            await handoff.take_over(
                s, admin_id=admin, instance_id=iid, session_id=sid, actor_user_id=admin
            )
        assert excinfo.value.session_id == sid
        assert str(excinfo.value) == "session_ended"

    async with tenant_session(admin) as s:
        sess = (await s.execute(select(ConvSession).where(ConvSession.id == sid))).scalar_one()
        assert sess.mode == "ai"
        assert sess.human_controlled_since is None
    assert await _events(admin, "human_takeover_started") == []
