"""Age-based data-retention sweep (Architecture §3.4.10 / §4.4 / Decision #38, P0-4).

Runs against the real DB with RLS. Covers: rows older than the 1-year boundary are
hard-deleted and a data_retention_hard_delete audit event is emitted per class; rows
within the window survive; the sweep is idempotent; and it is tenant-scoped (never
touches another tenant's rows — reuses the isolation harness / assert_no_bypass).

The second half covers the OPT-IN lead auto-prune rule that rides the same sweep. It is a
different policy with a different owner: Legal §B5 makes captured Leads the Admin's own
business data, so the platform prunes them only for a tenant that set a window itself.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.audit.log import verify_chain
from app.db.session import tenant_session
from app.lifecycle.retention import expire_aged_data, prune_stale_leads, run_retention_sweep
from app.models.audit import AdminAuditLog
from app.models.conversation import Session, SessionSummary, Transcript
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.lead import Lead, LeadState
from sqlalchemy import func, select


async def _seed_tenant(admin_id: uuid.UUID) -> uuid.UUID:
    async with tenant_session(admin_id) as s:
        s.add(
            Account(
                id=admin_id,
                admin_id=admin_id,
                user_id=admin_id,
                email=f"{admin_id}@x.com",
                email_lower=f"{admin_id}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        iid = uuid.uuid4()
        s.add(
            Instance(
                id=iid,
                admin_id=admin_id,
                name="L",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
        return iid


async def _add_transcript(s, *, admin_id, instance_id, created_at, text="hi") -> None:
    s.add(
        Transcript(
            id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=uuid.uuid4(),
            role="lead",
            text=text,
            created_at=created_at,
        )
    )


async def _add_summary(s, *, admin_id, instance_id, created_at, summary="s") -> None:
    s.add(
        SessionSummary(
            id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            resolved_lead_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            summary=summary,
            created_at=created_at,
        )
    )


async def _add_session(s, *, admin_id, instance_id, created_at) -> None:
    """The parent conversation row. ``summary`` is conversation content in its own
    right, so it is on the same 1-year clock as the transcripts underneath it."""
    s.add(
        Session(
            id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            channel="web",
            summary="caller asked about pricing",
            created_at=created_at,
            started_at=created_at,
            last_message_at=created_at,
        )
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_expire_aged_data_deletes_old_keeps_recent_and_audits():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    now = datetime.now(UTC)
    old = now - timedelta(days=400)  # past the 365d boundary
    recent = now - timedelta(days=10)  # inside the window
    async with tenant_session(admin) as s:
        await _add_transcript(s, admin_id=admin, instance_id=iid, created_at=old)
        await _add_transcript(s, admin_id=admin, instance_id=iid, created_at=recent)
        await _add_summary(s, admin_id=admin, instance_id=iid, created_at=old)
        await _add_summary(s, admin_id=admin, instance_id=iid, created_at=recent)
        await _add_session(s, admin_id=admin, instance_id=iid, created_at=old)
        await _add_session(s, admin_id=admin, instance_id=iid, created_at=recent)
        await s.flush()

    async with tenant_session(admin) as s:
        counts = await expire_aged_data(s, admin_id=admin, now=now)
        assert counts == {
            "transcript": 1,
            "session_summary": 1,
            "escalation_event": 0,
            "session": 1,
            "deferred_action_terminal": 0,
            "processed_stripe_event": 0,
            "tool_trace_event": 0,
            "retrieval_trace_event": 0,
            # Owner-deleted knowledge past its 30-day undo window (audit F109).
            "knowledge_source_grace_expired": 0,
            # Stored originals with no knowledge_sources row (round 7 WP-8).
            "knowledge_original_orphan": 0,
        }

    async with tenant_session(admin) as s:
        # Exactly the recent rows survive.
        t = (await s.execute(select(func.count()).select_from(Transcript))).scalar_one()
        ss = (await s.execute(select(func.count()).select_from(SessionSummary))).scalar_one()
        sess = (await s.execute(select(func.count()).select_from(Session))).scalar_one()
        assert t == 1 and ss == 1 and sess == 1
        # A data_retention_hard_delete event per class, and the chain stays intact.
        evts = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "data_retention_hard_delete",
                    )
                )
            )
            .scalars()
            .all()
        )
        classes = {(e.event_payload or {}).get("data_class") for e in evts}
        assert {"transcript", "session_summary", "session"} <= classes
        assert await verify_chain(s, admin) is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_expire_aged_data_is_idempotent():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    now = datetime.now(UTC)
    old = now - timedelta(days=400)
    async with tenant_session(admin) as s:
        await _add_transcript(s, admin_id=admin, instance_id=iid, created_at=old)
        await _add_summary(s, admin_id=admin, instance_id=iid, created_at=old)
        await _add_session(s, admin_id=admin, instance_id=iid, created_at=old)
        await s.flush()

    async with tenant_session(admin) as s:
        first = await expire_aged_data(s, admin_id=admin, now=now)
        assert first == {
            "transcript": 1,
            "session_summary": 1,
            "escalation_event": 0,
            "session": 1,
            "deferred_action_terminal": 0,
            "processed_stripe_event": 0,
            "tool_trace_event": 0,
            "retrieval_trace_event": 0,
            # Owner-deleted knowledge past its 30-day undo window (audit F109).
            "knowledge_source_grace_expired": 0,
            # Stored originals with no knowledge_sources row (round 7 WP-8).
            "knowledge_original_orphan": 0,
        }
    # Re-run: nothing left aged → zero deletes (idempotent).
    async with tenant_session(admin) as s:
        second = await expire_aged_data(s, admin_id=admin, now=now)
        assert second == {
            "transcript": 0,
            "session_summary": 0,
            "escalation_event": 0,
            "session": 0,
            "deferred_action_terminal": 0,
            "processed_stripe_event": 0,
            "tool_trace_event": 0,
            "retrieval_trace_event": 0,
            # Owner-deleted knowledge past its 30-day undo window (audit F109).
            "knowledge_source_grace_expired": 0,
            # Stored originals with no knowledge_sources row (round 7 WP-8).
            "knowledge_original_orphan": 0,
        }


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_retention_sweep_is_tenant_scoped():
    """The sweep on tenant A must NEVER touch tenant B's aged rows (RLS + app filter)."""
    admin_a, admin_b = uuid.uuid4(), uuid.uuid4()
    iid_a = await _seed_tenant(admin_a)
    iid_b = await _seed_tenant(admin_b)
    now = datetime.now(UTC)
    old = now - timedelta(days=400)
    async with tenant_session(admin_a) as s:
        await _add_transcript(s, admin_id=admin_a, instance_id=iid_a, created_at=old)
        await s.flush()
    async with tenant_session(admin_b) as s:
        await _add_transcript(s, admin_id=admin_b, instance_id=iid_b, created_at=old)
        await s.flush()

    # Sweep ONLY tenant A.
    swept = await run_retention_sweep([admin_a], now=now)
    assert str(admin_a) in swept

    # Tenant A's aged transcript is gone; tenant B's is untouched.
    async with tenant_session(admin_a) as s:
        assert (await s.execute(select(func.count()).select_from(Transcript))).scalar_one() == 0
    async with tenant_session(admin_b) as s:
        assert (await s.execute(select(func.count()).select_from(Transcript))).scalar_one() == 1


# --- the Admin's OPT-IN lead auto-prune rule (§3.4.10a, Legal §B5) ----------- #


async def _add_lead(
    s, *, admin_id, instance_id, last_activity_at, state=LeadState.active
) -> uuid.UUID:
    lid = uuid.uuid4()
    s.add(
        Lead(
            id=lid,
            admin_id=admin_id,
            instance_id=instance_id,
            name="L",
            contact_identifier="l@x.com",
            state=state,
            last_activity_at=last_activity_at,
        )
    )
    return lid


async def _set_retention(admin_id: uuid.UUID, days: int | None) -> None:
    async with tenant_session(admin_id) as s:
        inst = (await s.execute(select(Instance).where(Instance.admin_id == admin_id))).scalar_one()
        inst.lead_retention_days = days
        await s.flush()


async def _lead_ids(admin_id: uuid.UUID) -> set[uuid.UUID]:
    async with tenant_session(admin_id) as s:
        return set((await s.execute(select(Lead.id))).scalars().all())


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_lead_auto_prune_deletes_inactive_and_keeps_active():
    """Staleness is INACTIVITY, not age (§3.4.10a says "inactive for N months"), so the
    cutoff is measured against last_activity_at — a long-running lead that is still being
    worked must survive a window shorter than its own age."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    now = datetime.now(UTC)
    await _set_retention(admin, 30)

    async with tenant_session(admin) as s:
        stale = await _add_lead(
            s, admin_id=admin, instance_id=iid, last_activity_at=now - timedelta(days=45)
        )
        fresh = await _add_lead(
            s, admin_id=admin, instance_id=iid, last_activity_at=now - timedelta(days=5)
        )
        await s.flush()

    async with tenant_session(admin) as s:
        assert await prune_stale_leads(s, admin_id=admin, now=now) == 1

    assert await _lead_ids(admin) == {fresh}
    assert stale not in await _lead_ids(admin)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_no_rule_prunes_nothing():
    """NULL is off and off is the DEFAULT: captured Leads are the Admin's own business
    data, so the platform never timer-deletes them unless the Admin asked it to (§B5)."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    now = datetime.now(UTC)

    async with tenant_session(admin) as s:
        await _add_lead(
            s, admin_id=admin, instance_id=iid, last_activity_at=now - timedelta(days=4000)
        )
        await s.flush()

    async with tenant_session(admin) as s:
        assert await prune_stale_leads(s, admin_id=admin, now=now) == 0
    assert len(await _lead_ids(admin)) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_archived_leads_are_exempt_from_the_prune():
    """§B5 offers archive as the ALTERNATIVE to deletion, so archiving a lead cannot be a
    slower route to losing it."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    now = datetime.now(UTC)
    await _set_retention(admin, 30)

    async with tenant_session(admin) as s:
        archived = await _add_lead(
            s,
            admin_id=admin,
            instance_id=iid,
            last_activity_at=now - timedelta(days=400),
            state=LeadState.archived,
        )
        await s.flush()

    async with tenant_session(admin) as s:
        assert await prune_stale_leads(s, admin_id=admin, now=now) == 0
    assert await _lead_ids(admin) == {archived}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_auto_prune_audits_by_hashed_id_and_is_idempotent():
    """The audit trail must record the deletion without resurrecting the identifier it
    just destroyed (§3.4.11 / §5.3), which is why the id is hashed."""
    from app.audit.log import hash_lead_id

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    now = datetime.now(UTC)
    await _set_retention(admin, 30)

    async with tenant_session(admin) as s:
        stale = await _add_lead(
            s, admin_id=admin, instance_id=iid, last_activity_at=now - timedelta(days=45)
        )
        await s.flush()

    async with tenant_session(admin) as s:
        assert await prune_stale_leads(s, admin_id=admin, now=now) == 1
    # Nothing stale left → a second sweep is a no-op.
    async with tenant_session(admin) as s:
        assert await prune_stale_leads(s, admin_id=admin, now=now) == 0

    async with tenant_session(admin) as s:
        evts = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "lead_pruned",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(evts) == 1
        payload = evts[0].event_payload or {}
        assert payload["resolved_lead_id_hash"] == hash_lead_id(stale)
        assert str(stale) not in json.dumps(payload)
        assert payload["retention_policy_applied"] == "lead_auto_prune:30d"
        assert await verify_chain(s, admin) is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_retention_sweep_applies_each_tenant_s_own_rule():
    """One sweep, two policies: the platform-mandated transcript expiry runs for everyone,
    while the lead prune runs only for the tenant that opted in."""
    admin_on, admin_off = uuid.uuid4(), uuid.uuid4()
    iid_on = await _seed_tenant(admin_on)
    iid_off = await _seed_tenant(admin_off)
    now = datetime.now(UTC)
    await _set_retention(admin_on, 30)

    for admin, iid in ((admin_on, iid_on), (admin_off, iid_off)):
        async with tenant_session(admin) as s:
            await _add_lead(
                s, admin_id=admin, instance_id=iid, last_activity_at=now - timedelta(days=45)
            )
            await s.flush()

    swept = await run_retention_sweep([admin_on, admin_off], now=now)
    assert swept[str(admin_on)]["lead"] == 1
    assert str(admin_off) not in swept  # nothing swept at all for the opted-out tenant

    assert await _lead_ids(admin_on) == set()
    assert len(await _lead_ids(admin_off)) == 1


# --------------------------------------------------------------------------- #
# Data-growth enforcers (owner audit): tables that previously grew forever
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_terminal_deferred_actions_are_pruned_but_the_queue_is_never_touched() -> None:
    from app.models.deferred import DeferredAction

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    old = datetime.now(UTC) - timedelta(days=120)
    async with tenant_session(admin) as s:
        for status in ("delivered", "abandoned", "queued"):
            s.add(
                DeferredAction(
                    id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    action_type="schedule_callback",
                    connection_type="sms_sender",
                    payload={},
                    status=status,
                    created_at=old,
                    next_attempt_at=old,
                    expires_at=old + timedelta(hours=24),
                )
            )
    async with tenant_session(admin) as s:
        counts = await expire_aged_data(s, admin_id=admin)
        assert counts["deferred_action_terminal"] == 2
    async with tenant_session(admin) as s:
        left = (
            (await s.execute(select(DeferredAction.status).where(DeferredAction.admin_id == admin)))
            .scalars()
            .all()
        )
        # The 120-day-old QUEUED row survives: a pending promise is not aged data.
        assert left == ["queued"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_aged_escalation_events_and_stripe_replay_ids_are_pruned() -> None:
    from app.models.billing import ProcessedStripeEvent
    from app.models.conversation import EscalationEvent

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    now = datetime.now(UTC)
    async with tenant_session(admin) as s:
        s.add(
            EscalationEvent(
                admin_id=admin,
                instance_id=iid,
                signal="cannot_answer",
                gate="intake",
                fired_at=now - timedelta(days=400),
                created_at=now - timedelta(days=400),
            )
        )
        s.add(
            EscalationEvent(
                admin_id=admin,
                instance_id=iid,
                signal="high_value_lead",
                gate="outcome",
                fired_at=now - timedelta(days=10),
                created_at=now - timedelta(days=10),
            )
        )
        s.add(
            ProcessedStripeEvent(
                event_id="evt_old", admin_id=admin, created_at=now - timedelta(days=500)
            )
        )
        s.add(
            ProcessedStripeEvent(
                event_id="evt_recent", admin_id=admin, created_at=now - timedelta(days=30)
            )
        )
    async with tenant_session(admin) as s:
        counts = await expire_aged_data(s, admin_id=admin)
        assert counts["escalation_event"] == 1
        assert counts["processed_stripe_event"] == 1
    async with tenant_session(admin) as s:
        signals = (
            (
                await s.execute(
                    select(EscalationEvent.signal).where(EscalationEvent.admin_id == admin)
                )
            )
            .scalars()
            .all()
        )
        assert signals == ["high_value_lead"]  # the recent one survives
        ids = (
            (
                await s.execute(
                    select(ProcessedStripeEvent.event_id).where(
                        ProcessedStripeEvent.admin_id == admin
                    )
                )
            )
            .scalars()
            .all()
        )
        assert ids == ["evt_recent"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_platform_data_enforcers_prune_contact_forms_and_cap_the_access_log() -> None:
    """The two platform-level stores that had NO deletion path at all: anonymous
    contact-form PII (1 year) and the internal-access log (3-year ceiling, with
    the §5.11 floor clamped in SQL so nothing younger can ever be pruned)."""
    from app.db.session import untenanted_session
    from app.lifecycle.retention import expire_platform_data
    from app.models.contact import ContactSubmission
    from app.models.internal_access import InternalAccessLog

    now = datetime.now(UTC)
    async with untenanted_session() as s:
        s.add(
            ContactSubmission(
                id=uuid.uuid4(),
                name="Old Visitor",
                email="old@example.com",
                message="hello from long ago",
                created_at=now - timedelta(days=400),
            )
        )
        s.add(
            ContactSubmission(
                id=uuid.uuid4(),
                name="Recent Visitor",
                email="recent@example.com",
                message="hello",
                created_at=now - timedelta(days=10),
            )
        )

    # The app role is INSERT-only on internal_access_log (no-UPDATE trigger,
    # migration 0012), so aged entries are seeded as inserts with an explicit
    # created_at — exactly how a 4-year-old real entry looks to the pruner.
    admin = uuid.uuid4()
    async with untenanted_session() as s:
        s.add(
            InternalAccessLog(
                staff_id="ops@vantagemind",
                approver_id="second@vantagemind",
                admin_id=admin,
                data_class="conversation_content",
                access_started_at=now - timedelta(days=4 * 365),
                access_type="break_glass",
                justification="incident-123",
                ticket_ref="TICK-1",
                created_at=now - timedelta(days=4 * 365),
            )
        )
        s.add(
            InternalAccessLog(
                staff_id="ops@vantagemind",
                approver_id="second@vantagemind",
                admin_id=admin,
                data_class="conversation_content",
                access_started_at=now,
                access_type="break_glass",
                justification="incident-456",
                ticket_ref="TICK-2",
            )
        )

    counts = await expire_platform_data()
    assert counts.get("contact_submission") == 1
    assert counts.get("internal_access_log") == 1

    async with untenanted_session() as s:
        emails = (await s.execute(select(ContactSubmission.email))).scalars().all()
        assert emails == ["recent@example.com"]
        # The app role cannot read internal_access_log (§5.11 — reading the
        # compliance record needs its own approval), so the fresh entry's
        # survival is proven through the pruner itself: an AGGRESSIVE cutoff of
        # "now" is clamped to the 3-year floor in SQL and prunes NOTHING more.
        from sqlalchemy import text as sa_text

        pruned_again = (
            await s.execute(sa_text("SELECT internal_access_prune(:cutoff)"), {"cutoff": now})
        ).scalar_one()
        assert int(pruned_again or 0) == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_retention_reaps_orphaned_knowledge_originals_and_keeps_owned_ones():
    """Round 7 WP-8: the originals bucket has NO expiration by design, so an object
    whose knowledge_sources row is gone must be reaped by the sweep — and ONLY that
    object, and only once it is older than ORIGINAL_ORPHAN_MIN_AGE (an upload keeps
    its bytes before its row commits). A live source's file, a tombstoned source's
    undo payload and another tenant's prefix all survive; the reap is audited once
    and is idempotent."""
    from app.knowledge.original_store import (
        LocalOriginalStore,
        original_key_for,
        set_original_store,
    )
    from app.knowledge.service import DELETED_BY_OWNER
    from app.lifecycle.retention import ORIGINAL_ORPHAN_MIN_AGE
    from app.models.knowledge import KnowledgeSource

    LocalOriginalStore.reset()
    store = LocalOriginalStore()
    set_original_store(store)
    try:
        admin, other = uuid.uuid4(), uuid.uuid4()
        iid = await _seed_tenant(admin)
        await _seed_tenant(other)
        uploaded_at = datetime.now(UTC)
        live_id, tombstoned_id, gone_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        live_key = original_key_for(admin, live_id, "live.pdf")
        undo_key = original_key_for(admin, tombstoned_id, "undo.pdf")
        orphan_key = original_key_for(admin, gone_id, "orphan.pdf")  # no row: a failed delete
        theirs_key = original_key_for(other, uuid.uuid4(), "theirs.pdf")
        async with tenant_session(admin) as s:
            s.add(
                KnowledgeSource(
                    id=live_id,
                    admin_id=admin,
                    instance_id=iid,
                    name="live.pdf",
                    origin="upload",
                    original_key=live_key,
                )
            )
            s.add(
                KnowledgeSource(
                    id=tombstoned_id,
                    admin_id=admin,
                    instance_id=iid,
                    name="undo.pdf",
                    origin="upload",
                    original_key=undo_key,
                    # One day into the 30-day undo window: the row stays, so must its file.
                    soft_deleted_at=uploaded_at - timedelta(days=1),
                    deleted_by=DELETED_BY_OWNER,
                )
            )
            await s.flush()
        for key in (live_key, undo_key, orphan_key, theirs_key):
            await store.put(key=key, data=b"%PDF", content_type="application/pdf")

        # A sweep landing right after the upload sees a key with no row — the
        # in-flight shape — and must leave it alone (nothing reaped, nothing audited).
        async with tenant_session(admin) as s:
            fresh = await expire_aged_data(s, admin_id=admin, now=uploaded_at)
        assert fresh["knowledge_original_orphan"] == 0
        assert {live_key, undo_key, orphan_key, theirs_key} <= set(LocalOriginalStore.keys())

        # Past the minimum age (the sweep's clock moved on; the tombstone is still
        # well inside its undo window) the orphan — and only the orphan — goes.
        now = uploaded_at + ORIGINAL_ORPHAN_MIN_AGE + timedelta(hours=1)
        async with tenant_session(admin) as s:
            counts = await expire_aged_data(s, admin_id=admin, now=now)
        assert counts["knowledge_original_orphan"] == 1
        assert counts["knowledge_source_grace_expired"] == 0
        remaining = set(LocalOriginalStore.keys())
        assert orphan_key not in remaining
        assert {live_key, undo_key, theirs_key} <= remaining

        async with tenant_session(admin) as s:
            evts = (
                (
                    await s.execute(
                        select(AdminAuditLog).where(
                            AdminAuditLog.admin_id == admin,
                            AdminAuditLog.event_type == "data_retention_hard_delete",
                        )
                    )
                )
                .scalars()
                .all()
            )
            payloads = [e.event_payload or {} for e in evts]
            assert [p_["data_class"] for p_ in payloads] == ["knowledge_original"]
            assert payloads[0]["count"] == 1 and payloads[0]["delete_errors"] == 0
            assert (
                payloads[0]["retention_policy_applied"]
                == "orphan_reconcile:no_knowledge_sources_row"
            )
            assert await verify_chain(s, admin) is True

        # Idempotent: the second pass finds nothing orphaned and writes no audit row.
        async with tenant_session(admin) as s:
            again = await expire_aged_data(s, admin_id=admin, now=now)
        assert again["knowledge_original_orphan"] == 0
        async with tenant_session(admin) as s:
            n = (
                await s.execute(
                    select(func.count())
                    .select_from(AdminAuditLog)
                    .where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "data_retention_hard_delete",
                    )
                )
            ).scalar_one()
            assert n == 1
        assert {live_key, undo_key, theirs_key} <= set(LocalOriginalStore.keys())
    finally:
        set_original_store(None)
        LocalOriginalStore.reset()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_orphan_reap_survives_a_store_outage_without_losing_row_retention():
    """A listing failure must not roll back the tenant's row deletions: it is logged
    at ERROR (the sweep alarm reads that) and the run reaps nothing."""
    from app.knowledge.original_store import set_original_store

    class _DownStore:
        async def put(self, *, key, data, content_type):  # pragma: no cover - unused
            raise AssertionError

        async def get(self, *, key):  # pragma: no cover - unused
            return None

        async def delete(self, *, key):  # pragma: no cover - unused
            raise AssertionError

        async def delete_prefix(self, *, prefix):  # pragma: no cover - unused
            return 0

        async def list_prefix(self, *, prefix):
            raise OSError("bucket unreachable")

    set_original_store(_DownStore())
    try:
        admin = uuid.uuid4()
        iid = await _seed_tenant(admin)
        now = datetime.now(UTC)
        async with tenant_session(admin) as s:
            await _add_transcript(
                s, admin_id=admin, instance_id=iid, created_at=now - timedelta(days=400)
            )
            await s.flush()
        async with tenant_session(admin) as s:
            counts = await expire_aged_data(s, admin_id=admin, now=now)
        assert counts["transcript"] == 1  # the row retention still happened
        assert counts["knowledge_original_orphan"] == 0
    finally:
        set_original_store(None)
