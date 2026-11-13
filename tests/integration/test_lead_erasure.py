"""Right-to-erasure cascade (§3.4.11) — 2026-09-05 audit F091 / F018 / F096.

Every lead-destroying path (dashboard erase, dashboard prune, opt-in auto-prune)
runs ONE cascade, and that cascade takes everything the platform holds about the
person: hot transcripts, the cold-storage transcript object, session summaries,
the session's own summary text / transport key / lead link, the voice record's
caller number, queued follow-ups, CRM link rows, and the lead.

The rows the erasure keeps but anonymises (sessions, voice call records) are walked
column by column at the bottom of this file: every column of the model is either
cleared by the erase (and verified so) or listed as surviving it, with the reason. A
column added later fails that walk until someone decides which it is — the reply
context added by migration 0066 was the customer's identifier in the clear and rode
through every erasure because nothing forced that decision. Transcript rows are
DELETED by the erase (hot rows and the cold object), so nothing of them is left to
walk.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.api.v1 import dashboard
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

pytestmark = [pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")]


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


# --- the reply context and the parked answer (round 7 phase 2 review, seams 1) ----------

WA_NUMBER = "16045550100"
PSID = "psid-7781"
SUBJECT = "Quote for the kitchen at 12 Elm"
MESSAGE_ID = "<CAF123@mail.jane.example>"
PARKED = {
    "channel": "email",
    "body": "Here is the quote you asked for.",
    "candidate": "jane@x.com",
    "asked_at": "2026-10-01T00:00:00+00:00",
}


async def _add_channel_sessions(
    admin: uuid.UUID, iid: uuid.UUID, lead_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """What inbound_dispatch.persist_reply_context leaves on a WhatsApp and an email
    session of the lead, plus an answer parked for another channel — and a second
    person's Messenger session, which must come through the erase untouched."""
    wa, mail, neighbour = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Session(
                id=wa,
                admin_id=admin,
                instance_id=iid,
                channel="whatsapp",
                mode="human",
                participant_key="a" * 64,
                resolved_lead_id=lead_id,
                meta_sender_id=WA_NUMBER,
                meta_destination_id="pn-1",
                pending_outbound=dict(PARKED),
            )
        )
        s.add(
            Session(
                id=mail,
                admin_id=admin,
                instance_id=iid,
                channel="email",
                mode="ai",
                participant_key="b" * 64,
                resolved_lead_id=lead_id,
                inbound_subject=SUBJECT,
                inbound_message_id=MESSAGE_ID,
            )
        )
        s.add(
            Session(
                id=neighbour,
                admin_id=admin,
                instance_id=iid,
                channel="messenger",
                mode="human",
                participant_key="c" * 64,
                meta_sender_id=PSID,
                meta_destination_id="page-9",
                inbound_subject="kept",
                inbound_message_id="<kept@other.example>",
                pending_outbound={**PARKED, "candidate": "other@x.com"},
            )
        )
        await s.flush()
    return wa, mail, neighbour


async def _erase(admin: uuid.UUID, lead_id: uuid.UUID):
    async with tenant_session(admin) as s:
        lead = (await s.execute(select(Lead).where(Lead.id == lead_id))).scalar_one()
        return await erase_lead_data(
            s,
            admin_id=admin,
            lead=lead,
            event_type="lead_erasure",
            actor_type=ActorType.admin_user,
            actor_user_id=admin,
        )


async def _reply_context(admin: uuid.UUID, sid: uuid.UUID) -> tuple[object, ...]:
    async with tenant_session(admin) as s:
        sess = (await s.execute(select(Session).where(Session.id == sid))).scalar_one()
        return (
            sess.inbound_subject,
            sess.inbound_message_id,
            sess.meta_sender_id,
            sess.meta_destination_id,
            sess.pending_outbound,
        )


async def test_the_erase_clears_the_reply_context_and_the_parked_answer(cold_store) -> None:
    """Migration 0066 put the customer's WhatsApp number / PSID, their subject line and
    their Message-ID on the session row so a human reply can be addressed; 0063 parks
    an answer with the address they gave. The erase kept the row and all of it."""
    admin = uuid.uuid4()
    iid, lead_id, _ = await _seed_person(admin)
    wa, mail, neighbour = await _add_channel_sessions(admin, iid, lead_id)
    assert await _reply_context(admin, wa) == (None, None, WA_NUMBER, "pn-1", PARKED)
    assert await _reply_context(admin, mail) == (SUBJECT, MESSAGE_ID, None, None, None)

    counts = await _erase(admin, lead_id)

    assert counts.sessions_anonymised == 3
    assert await _reply_context(admin, wa) == (None, None, None, None, None)
    assert await _reply_context(admin, mail) == (None, None, None, None, None)
    # Somebody else's conversation is not this person's data.
    assert await _reply_context(admin, neighbour) == (
        "kept",
        "<kept@other.example>",
        PSID,
        "page-9",
        {**PARKED, "candidate": "other@x.com"},
    )


async def test_auto_prune_clears_the_reply_context_too(cold_store) -> None:
    admin = uuid.uuid4()
    iid, lead_id, _ = await _seed_person(admin, lead_retention_days=90, stale=True)
    wa, mail, _ = await _add_channel_sessions(admin, iid, lead_id)
    async with tenant_session(admin) as s:
        assert await retention.prune_stale_leads(s, admin_id=admin) == 1
    assert await _reply_context(admin, wa) == (None, None, None, None, None)
    assert await _reply_context(admin, mail) == (None, None, None, None, None)


async def test_a_takeover_reply_has_no_recipient_once_the_person_is_erased(cold_store) -> None:
    """The residue was operative, not inert: on a still-open Meta session the takeover
    reply is addressed from ``sessions.meta_sender_id`` alone, so the owner could still
    message the erased person. With the context gone there is nobody to send to."""
    admin = uuid.uuid4()
    iid, lead_id, _ = await _seed_person(admin)
    wa, _, neighbour = await _add_channel_sessions(admin, iid, lead_id)

    async def recipient(sid: uuid.UUID) -> str | None:
        async with tenant_session(admin) as s:
            sess = (await s.execute(select(Session).where(Session.id == sid))).scalar_one()
            return await dashboard._takeover_recipient(s, admin_id=admin, sess=sess)

    assert await recipient(wa) == WA_NUMBER
    await _erase(admin, lead_id)
    assert await recipient(wa) is None
    assert await recipient(neighbour) == PSID


# --- schema walk: every column of an anonymised row is a decision ------------------------

# Columns the erase leaves on the session row, each with the reason it is not the
# person's data. Adding a column to the model fails the walk below until it is listed
# here, or cleared in app/lifecycle/lead_erasure.py and listed in _SESSION_CLEARED.
_SESSION_SURVIVES: dict[str, str] = {
    "id": "the row's own random key; names no one",
    "admin_id": "the tenant scope",
    "created_at": "row timing, part of the billing/analytics record",
    "instance_id": "which Luciel served it",
    "channel": "which transport carried it, not who was on it",
    "mode": "ai/human state of the conversation",
    "counted": "deprecated billing flag, never read",
    "started_at": "timing of the billed conversation",
    "last_message_at": "timing of the billed conversation",
    "ended_at": "timing of the billed conversation",
    "transcripts_archived_at": "when the transcript moved to cold storage; the object is deleted",
    "config_snapshot": "the TENANT's frozen config (tools, channels, tone); no customer input",
    "input_ask_attempts": "a counter of stalled turns",
    "human_controlled_since": "when the owner took over",
}

# Columns the erase must clear, with the value seeded so the walk can prove each was
# populated before and is NULL after (``resolved_lead_id`` is filled in by the test).
_SESSION_CLEARED: dict[str, object] = {
    "resolved_lead_id": None,
    "participant_key": "d" * 64,
    "archive_object_key": "vm/tenant/transcripts/instance/session.jsonl",
    "summary": "Jane asked for a quote and left her number",
    "pending_outbound": dict(PARKED),
    "inbound_subject": SUBJECT,
    "inbound_message_id": MESSAGE_ID,
    "meta_destination_id": "pn-1",
    "meta_sender_id": WA_NUMBER,
    "transport_sender": "+16045550100",  # the raw sender persisted at ingress (0074)
}

_VOICE_SURVIVES: dict[str, str] = {
    "id": "the row's own random key",
    "admin_id": "the tenant scope",
    "created_at": "row timing",
    "instance_id": "which Luciel served it",
    "provider_call_id": "the provider's call id the disclosure proof is filed under",
    "session_id": "links the proof to the anonymised session row",
    "disclosure_played_at": "compliance evidence that the disclosure was played",
}
# Scrubbed to the empty string (the column is NOT NULL), not to NULL.
_VOICE_CLEARED = {"from_number"}


def _unclassified(model, *groups) -> tuple[set[str], set[str]]:
    columns = {c.name for c in model.__table__.columns}
    listed: set[str] = set()
    for group in groups:
        assert not listed & set(group), f"listed twice: {sorted(listed & set(group))}"
        listed |= set(group)
    return columns - listed, listed - columns


async def test_every_session_column_is_classified_for_erasure() -> None:
    missing, stale = _unclassified(Session, _SESSION_SURVIVES, _SESSION_CLEARED)
    assert not missing, (
        f"sessions column(s) {sorted(missing)} are not classified for lead erasure: clear "
        "them in app/lifecycle/lead_erasure.py and add them to _SESSION_CLEARED, or add "
        "them to _SESSION_SURVIVES with the reason they hold nothing about the person"
    )
    assert not stale, f"no longer on the model: {sorted(stale)}"
    assert all(reason.strip() for reason in _SESSION_SURVIVES.values())


async def test_every_voice_record_column_is_classified_for_erasure() -> None:
    missing, stale = _unclassified(VoiceCallRecord, _VOICE_SURVIVES, _VOICE_CLEARED)
    assert not missing, (
        f"voice_call_records column(s) {sorted(missing)} are not classified for lead "
        "erasure: scrub them in app/lifecycle/lead_erasure.py and add them to "
        "_VOICE_CLEARED, or add them to _VOICE_SURVIVES with a reason"
    )
    assert not stale, f"no longer on the model: {sorted(stale)}"
    assert all(reason.strip() for reason in _VOICE_SURVIVES.values())


async def test_the_erase_clears_every_column_it_does_not_explicitly_keep(cold_store) -> None:
    """The walk itself, against a row with EVERY column populated: what is listed as
    cleared was there before and is NULL after; what is listed as kept is unchanged."""
    admin = uuid.uuid4()
    iid, lead_id, _ = await _seed_person(admin)
    sid = uuid.uuid4()
    now = datetime.now(UTC)
    seeded = {**_SESSION_CLEARED, "resolved_lead_id": lead_id}
    async with tenant_session(admin) as s:
        s.add(
            Session(
                id=sid,
                admin_id=admin,
                instance_id=iid,
                channel="whatsapp",
                mode="human",
                counted=False,
                started_at=now - timedelta(hours=2),
                last_message_at=now - timedelta(hours=1),
                ended_at=now,
                transcripts_archived_at=now,
                config_snapshot={"enabled_tools": [], "personality": {"preset": "warm"}},
                input_ask_attempts=2,
                human_controlled_since=now - timedelta(minutes=30),
                **seeded,
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
        await s.flush()

    async def row() -> dict[str, object]:
        async with tenant_session(admin) as s:
            sess = (await s.execute(select(Session).where(Session.id == sid))).scalar_one()
            return {c.name: getattr(sess, c.name) for c in Session.__table__.columns}

    async def voice_row() -> dict[str, object]:
        async with tenant_session(admin) as s:
            rec = (
                await s.execute(select(VoiceCallRecord).where(VoiceCallRecord.session_id == sid))
            ).scalar_one()
            return {c.name: getattr(rec, c.name) for c in VoiceCallRecord.__table__.columns}

    before, voice_before = await row(), await voice_row()
    unseeded = sorted(name for name, value in before.items() if value is None)
    assert not unseeded, f"the walk needs every column populated; NULL before: {unseeded}"

    await _erase(admin, lead_id)

    after, voice_after = await row(), await voice_row()
    for name in before:
        if name in _SESSION_CLEARED:
            assert after[name] is None, f"sessions.{name} survived the erasure"
        else:
            assert after[name] == before[name], f"sessions.{name} changed by the erasure"
    for name in voice_before:
        if name in _VOICE_CLEARED:
            assert voice_before[name], f"voice.{name} was not seeded"
            assert voice_after[name] == "", f"voice.{name} survived the erasure"
        else:
            assert voice_after[name] == voice_before[name], f"voice.{name} changed"
