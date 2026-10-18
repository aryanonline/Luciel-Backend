"""Right-to-erasure cascade (§3.4.11) — 2026-09-05 audit F091 / F018 / F096.

Every lead-destroying path (dashboard erase, dashboard prune, opt-in auto-prune)
runs ONE cascade, and that cascade takes everything the platform holds about the
person: hot transcripts, the cold-storage transcript object, session summaries,
the session's own summary text / transport key / lead link, the voice record's
caller number, queued follow-ups, CRM link rows, and the lead.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.lifecycle import retention
from app.lifecycle.lead_erasure import erase_lead_data
from app.lifecycle.transcript_store import LocalTranscriptStore, set_transcript_store
from app.models.audit import ActorType, AdminAuditLog
from app.models.conversation import Session, SessionSummary, Transcript
from app.models.deferred import DeferredAction
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.lead import Lead, LeadState
from app.models.voice import VoiceCallRecord
from sqlalchemy import func, select

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")]


@pytest.fixture
def cold_store():
    LocalTranscriptStore.reset()
    store = LocalTranscriptStore()
    set_transcript_store(store)
    yield store
    set_transcript_store(None)
    LocalTranscriptStore.reset()


async def _seed_person(
    admin: uuid.UUID, *, lead_retention_days: int | None = None, stale: bool = False
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """An account with a Luciel, a lead, one session resolved to the lead (hot rows,
    a cold object, a summary, a voice record, a queued callback) and one unrelated
    session that must survive."""
    iid, lead_id, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    other_sid = uuid.uuid4()
    now = datetime.now(UTC)
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@erase.test",
                email_lower=f"{admin}@erase.test",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="L",
                state=LucielState.active,
                lead_retention_days=lead_retention_days,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        s.add(
            Lead(
                id=lead_id,
                admin_id=admin,
                instance_id=iid,
                name="Jane",
                contact_identifier="+16045550100",
                email="jane@x.com",
                state=LeadState.active,
                last_activity_at=now - timedelta(days=400 if stale else 1),
            )
        )
        s.add(
            Session(
                id=sid,
                admin_id=admin,
                instance_id=iid,
                channel="sms",
                mode="ai",
                participant_key="+16045550100",
                # The raw sender the SMS transport carried (migration 0074).
                transport_sender="+16045550100",
                resolved_lead_id=lead_id,
                summary="Jane asked for a quote",
                archive_object_key=f"vm/{admin}/transcripts/{iid}/{sid}.jsonl",
                ended_at=now,
            )
        )
        s.add(
            Session(
                id=other_sid,
                admin_id=admin,
                instance_id=iid,
                channel="widget",
                mode="ai",
                summary="someone else",
            )
        )
        s.add(
            Transcript(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                session_id=sid,
                role="customer",
                text="my number is +16045550100",
            )
        )
        s.add(
            Transcript(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                session_id=other_sid,
                role="customer",
                text="unrelated",
            )
        )
        s.add(
            SessionSummary(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                session_id=sid,
                resolved_lead_id=lead_id,
                summary="Jane wants a quote",
            )
        )
        s.add(
            VoiceCallRecord(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                provider_call_id=f"CA{uuid.uuid4().hex}",
                from_number="+16045550100",
                session_id=sid,
            )
        )
        s.add(
            DeferredAction(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                action_type="schedule_callback",
                connection_type="sms_sender",
                resolved_lead_id=lead_id,
                payload={"to": "+16045550100", "text": "calling you back"},
                status="queued",
                expires_at=now + timedelta(days=1),
                next_attempt_at=now,
            )
        )
        await s.flush()
    return iid, lead_id, sid


async def _count(admin: uuid.UUID, model, *conds) -> int:
    async with tenant_session(admin) as s:
        return int(
            (
                await s.execute(
                    select(func.count()).select_from(model).where(model.admin_id == admin, *conds)
                )
            ).scalar_one()
        )


async def test_the_cascade_takes_everything_and_leaves_the_neighbour(cold_store) -> None:
    admin = uuid.uuid4()
    iid, lead_id, sid = await _seed_person(admin)
    key = f"vm/{admin}/transcripts/{iid}/{sid}.jsonl"
    await cold_store.put(key=key, data=b"archived words")

    async with tenant_session(admin) as s:
        lead = (await s.execute(select(Lead).where(Lead.id == lead_id))).scalar_one()
        counts = await erase_lead_data(
            s,
            admin_id=admin,
            lead=lead,
            event_type="lead_erasure",
            actor_type=ActorType.admin_user,
            actor_user_id=admin,
        )
    assert counts.transcripts_deleted == 1
    assert counts.archived_objects_deleted == 1
    assert counts.summaries_deleted == 1
    assert counts.sessions_anonymised == 1
    assert counts.voice_records_scrubbed == 1
    assert counts.deferred_actions_deleted == 1
    assert await cold_store.get(key=key) is None

    assert await _count(admin, Lead) == 0
    assert await _count(admin, Transcript, Transcript.session_id == sid) == 0
    assert await _count(admin, Transcript) == 1  # the neighbour's row survives
    assert await _count(admin, SessionSummary) == 0
    assert await _count(admin, DeferredAction) == 0
    async with tenant_session(admin) as s:
        sess = (await s.execute(select(Session).where(Session.id == sid))).scalar_one()
        assert (sess.summary, sess.participant_key, sess.resolved_lead_id) == (None, None, None)
        assert sess.archive_object_key is None
        # The plaintext sender the session held for a takeover reply goes too (0074).
        assert sess.transport_sender is None
        voice = (await s.execute(select(VoiceCallRecord))).scalar_one()
        assert voice.from_number == ""
        assert voice.disclosure_played_at is not None  # compliance proof stays
        event = (
            await s.execute(
                select(AdminAuditLog).where(
                    AdminAuditLog.admin_id == admin, AdminAuditLog.event_type == "lead_erasure"
                )
            )
        ).scalar_one()
        assert "jane" not in str(event.event_payload).lower()
        assert event.event_payload["archived_objects_deleted"] == 1


async def test_auto_prune_runs_the_same_cascade(cold_store) -> None:
    """F091: prune used to delete only the lead row."""
    admin = uuid.uuid4()
    iid, lead_id, sid = await _seed_person(admin, lead_retention_days=90, stale=True)
    async with tenant_session(admin) as s:
        assert await retention.prune_stale_leads(s, admin_id=admin) == 1
    assert await _count(admin, Lead) == 0
    assert await _count(admin, Transcript, Transcript.session_id == sid) == 0
    assert await _count(admin, SessionSummary) == 0
    async with tenant_session(admin) as s:
        event = (
            await s.execute(
                select(AdminAuditLog).where(
                    AdminAuditLog.admin_id == admin, AdminAuditLog.event_type == "lead_pruned"
                )
            )
        ).scalar_one()
    assert event.event_payload["retention_policy_applied"] == "lead_auto_prune:90d"
    assert event.event_payload["transcripts_deleted"] == 1


async def test_a_cold_store_failure_is_counted_and_logged_not_hidden(cold_store, caplog) -> None:
    import logging

    class _Broken(LocalTranscriptStore):
        async def delete(self, *, key: str) -> None:  # type: ignore[override]
            raise RuntimeError("s3 down")

    set_transcript_store(_Broken())
    admin = uuid.uuid4()
    _, lead_id, _ = await _seed_person(admin)
    with caplog.at_level(logging.ERROR, logger="app.lifecycle.lead_erasure"):
        async with tenant_session(admin) as s:
            lead = (await s.execute(select(Lead).where(Lead.id == lead_id))).scalar_one()
            counts = await erase_lead_data(
                s,
                admin_id=admin,
                lead=lead,
                event_type="lead_erasure",
                actor_type=ActorType.platform,
            )
    assert counts.archived_object_errors == 1
    assert await _count(admin, Lead) == 0  # the rest of the erasure still happened
    assert any("archived transcript" in r.message for r in caplog.records)
