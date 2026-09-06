"""Escalation delivery truth (2026-09-05 audit F053/F061/F065/F079/F083/F088/F089).

An escalation row is written ONCE per (session, signal, gate) — the insert is the
arbiter, so two turns racing on one session cannot page the owner twice. What happened
to the notification is recorded on the row (``delivery_status``), the owner fallback
honours the owner's own suppression, the configured contact keeps being retried after
the owner fallback without paging the owner again, the in-process ladder reaches the
contact minutes before the hourly sweep would, a notifier crash leaves the same trail a
refusal does, and ``/healthz`` carries the backlog.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.connections import deferred_queue
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.conversation import EscalationEvent
from app.models.deferred import DeferredAction
from app.models.email_incident import EmailDeliveryIncident
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.lead import Lead
from app.notifications import email_notifier, escalation_dispatch
from app.notifications.escalation_dispatch import (
    ESCALATION_RETRY_ACTION,
    notify_escalation_safely,
    replay_queued_notification,
)
from app.runtime.integration_status import escalation_backlog
from app.runtime.orchestrator import _record_escalation
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

OWNER_EMAIL = "owner@acme.example"
CONTACT = "sales@acme.example"


class SelectiveEmailSender:
    """SES as it misbehaves: refuses some recipients, or everyone, until told otherwise."""

    def __init__(self) -> None:
        self.refuse: set[str] | None = set()  # None → refuse everyone
        self.sent: list[str] = []

    async def send(self, *, to, subject, body_text, from_address=None, tags=None):
        if self.refuse is None or to in self.refuse:
            raise RuntimeError("throttled")
        self.sent.append(to)
        return f"ses-{len(self.sent)}"


@pytest.fixture
def ses(monkeypatch) -> SelectiveEmailSender:
    fake = SelectiveEmailSender()
    monkeypatch.setattr(email_notifier, "build_email_sender", lambda: fake)
    return fake


async def _seed(
    admin: uuid.UUID,
    iid: uuid.UUID,
    *,
    contact: str | None = CONTACT,
    owner_suppressed: bool = False,
) -> uuid.UUID:
    lead_id = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            EmailDeliveryIncident(
                admin_id=admin, email_lower=CONTACT, verified_at=datetime.now(UTC)
            )
        )
        if owner_suppressed:
            s.add(
                EmailDeliveryIncident(
                    admin_id=admin, email_lower=OWNER_EMAIL, hard_bounced_at=datetime.now(UTC)
                )
            )
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=uuid.uuid4(),
                email=OWNER_EMAIL,
                email_lower=OWNER_EMAIL,
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="Acme Patios",
                state=LucielState.active,
                escalation={"primaryEmail": contact, "preferredChannel": "email"}
                if contact
                else {},
            )
        )
        s.add(
            Lead(
                id=lead_id,
                admin_id=admin,
                instance_id=iid,
                name="Dana Lee",
                contact_identifier="dana@lead.example",
                intent="Wants a quote",
            )
        )
        await s.flush()
    return lead_id


async def _fire(admin: uuid.UUID, iid: uuid.UUID, sid: uuid.UUID, lead_id: uuid.UUID) -> bool:
    async with tenant_session(admin) as s:
        return await _record_escalation(
            s,
            admin_id=admin,
            instance_id=iid,
            session_id=sid,
            resolved_lead_id=lead_id,
            signal="high_value_lead",
            gate="outcome",
            score=0.91,
        )


async def _row(admin: uuid.UUID, sid: uuid.UUID) -> EscalationEvent:
    async with tenant_session(admin) as s:
        return (
            await s.execute(
                select(EscalationEvent).where(
                    EscalationEvent.admin_id == admin, EscalationEvent.session_id == sid
                )
            )
        ).scalar_one()


async def _rows(admin: uuid.UUID, sid: uuid.UUID) -> list[EscalationEvent]:
    async with tenant_session(admin) as s:
        return list(
            (
                await s.execute(
                    select(EscalationEvent).where(
                        EscalationEvent.admin_id == admin, EscalationEvent.session_id == sid
                    )
                )
            )
            .scalars()
            .all()
        )


async def _audit(admin: uuid.UUID, event_type: str) -> list[AdminAuditLog]:
    async with tenant_session(admin) as s:
        return list(
            (
                await s.execute(
                    select(AdminAuditLog)
                    .where(AdminAuditLog.admin_id == admin, AdminAuditLog.event_type == event_type)
                    .order_by(AdminAuditLog.seq.asc())
                )
            )
            .scalars()
            .all()
        )


async def _queued(admin: uuid.UUID) -> list[DeferredAction]:
    async with tenant_session(admin) as s:
        return list(
            (
                await s.execute(
                    select(DeferredAction).where(
                        DeferredAction.admin_id == admin,
                        DeferredAction.action_type == ESCALATION_RETRY_ACTION,
                    )
                )
            )
            .scalars()
            .all()
        )


# --- once per (session, signal, gate) --------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_escalation_is_recorded_and_paged_once_per_session_signal_gate(ses):
    ses.refuse = set()
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid)

    assert await _fire(admin, iid, sid, lead) is True
    assert await _fire(admin, iid, sid, lead) is False  # the insert is the arbiter

    assert len(await _rows(admin, sid)) == 1
    assert len(await _audit(admin, "escalation_fired")) == 1
    assert ses.sent == [CONTACT]  # paged once, never twice
    row = await _row(admin, sid)
    assert row.delivery_status == "delivered"
    assert row.delivery_detail == "email:primary"
    assert row.delivered_at is not None


# --- owner fallback honours the owner's own suppression ------------------------------ #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_suppressed_owner_address_is_not_paged_and_the_contact_is_queued(ses):
    ses.refuse = None  # provider down for everyone
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid, owner_suppressed=True)

    await _fire(admin, iid, sid, lead)

    assert ses.sent == []
    failures = await _audit(admin, "escalation_delivery_failed")
    reasons = {
        (f.event_payload.get("recipient_role"), f.event_payload.get("failure_reason"))
        for f in failures
    }
    assert ("owner_fallback", "owner_email_suppressed") in reasons
    assert (await _row(admin, sid)).delivery_status == "queued"
    queued = await _queued(admin)
    assert len(queued) == 1
    assert queued[0].payload["contacts_only"] is False  # nobody heard: full routing on replay


# --- the contact keeps being retried after the owner fallback ------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_owner_fallback_keeps_retrying_the_contact_without_paging_the_owner_again(ses):
    ses.refuse = {CONTACT}  # the configured contact is refused; the owner is reachable
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid)

    await _fire(admin, iid, sid, lead)
    assert ses.sent == [OWNER_EMAIL]
    row = await _row(admin, sid)
    assert row.delivery_status == "owner_fallback"
    queued = await _queued(admin)
    assert len(queued) == 1
    assert queued[0].payload["contacts_only"] is True

    # The provider recovers; the queued replay reaches the CONTACT and nobody else.
    ses.refuse = set()
    async with tenant_session(admin) as s:
        assert (
            await replay_queued_notification(
                s,
                queued[0].payload,
                admin_id=admin,
                instance_id=iid,
                resolved_lead_id=lead,
            )
            is True
        )
    assert ses.sent == [OWNER_EMAIL, CONTACT]
    row = await _row(admin, sid)
    assert row.delivery_status == "delivered"
    assert row.delivery_detail == "email:primary"
    assert len(await _audit(admin, "escalation_owner_fallback")) == 1


# --- in-process ladder -------------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_ladder_reaches_the_contact_and_the_queued_replay_stands_down(ses, monkeypatch):
    monkeypatch.setattr(escalation_dispatch, "ESCALATION_RETRY_DELAYS", (0.25,))
    ses.refuse = None
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid)

    await _fire(admin, iid, sid, lead)  # refused everywhere → queued + ladder scheduled
    assert (await _row(admin, sid)).delivery_status == "queued"
    ses.refuse = set()  # the blip clears before the first rung
    await escalation_dispatch.drain_background_retries()

    assert ses.sent == [CONTACT]
    assert (await _row(admin, sid)).delivery_status == "delivered"

    # The deferred item is still queued; its replay must not page anyone again.
    (item,) = await _queued(admin)
    async with tenant_session(admin) as s:
        assert (
            await replay_queued_notification(
                s, item.payload, admin_id=admin, instance_id=iid, resolved_lead_id=lead
            )
            is True
        )
    assert ses.sent == [CONTACT]


# --- a notifier crash leaves the same trail a refusal does ---------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_notifier_crash_is_audited_and_queued(ses, monkeypatch):
    async def _boom(*a, **kw):
        raise RuntimeError("template exploded")

    monkeypatch.setattr(escalation_dispatch, "notify_escalation", _boom)
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid)

    await _fire(admin, iid, sid, lead)  # goes through notify_escalation_safely

    failures = await _audit(admin, "escalation_delivery_failed")
    assert [f.event_payload["failure_reason"] for f in failures] == [
        "notifier_exception:RuntimeError"
    ]
    assert (await _row(admin, sid)).delivery_status == "queued"
    assert len(await _queued(admin)) == 1

    # And calling the safe wrapper directly never raises either.
    async with tenant_session(admin) as s:
        await notify_escalation_safely(
            s,
            admin_id=admin,
            instance_id=iid,
            session_id=sid,
            resolved_lead_id=lead,
            signal="cannot_answer",
            gate="outcome",
        )


# --- the abandon turns the row terminal --------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_abandoned_retry_marks_the_row_failed(ses):
    ses.refuse = None
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid, owner_suppressed=True)
    await _fire(admin, iid, sid, lead)
    (item,) = await _queued(admin)

    async with tenant_session(admin) as s:
        counts = await run_sweep_for(s, admin, now=item.expires_at + timedelta(minutes=1))
    assert counts["abandoned"] == 1
    row = await _row(admin, sid)
    assert row.delivery_status == "failed"
    assert row.delivery_detail == "abandoned_after_24h"


async def run_sweep_for(s, admin: uuid.UUID, *, now: datetime) -> dict[str, int]:
    return await deferred_queue.run_sweep(
        s, admin_id=admin, deliverer=deferred_queue.ChannelDeliverer(s), now=now
    )


# --- /healthz backlog ---------------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_backlog_counts_queued_retries_and_terminal_misses(ses):
    ses.refuse = None
    admin, iid = uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid, owner_suppressed=True)
    sid_queued, sid_failed = uuid.uuid4(), uuid.uuid4()
    # One escalation the queue gave up on (terminal miss) ...
    await _fire(admin, iid, sid_failed, lead)
    (failed_item,) = await _queued(admin)
    async with tenant_session(admin) as s:
        await run_sweep_for(s, admin, now=failed_item.expires_at + timedelta(minutes=1))
    # ... and one still waiting its retry.
    await _fire(admin, iid, sid_queued, lead)

    backlog = await escalation_backlog()
    assert backlog is not None
    assert backlog["queued"] >= 1
    assert backlog["failed24h"] >= 1
