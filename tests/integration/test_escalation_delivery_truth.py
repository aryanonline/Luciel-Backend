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

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
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
from app.notifications.backlog import DEFERRED_SWEEP_INTERVAL
from app.notifications.escalation_dispatch import (
    ESCALATION_RETRY_ACTION,
    notify_escalation_safely,
    replay_queued_notification,
)
from app.runtime.integration_status import escalation_backlog
from app.runtime.orchestrator import _record_escalation
from sqlalchemy import select, update

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
        written = await _record_escalation(
            s,
            admin_id=admin,
            instance_id=iid,
            session_id=sid,
            resolved_lead_id=lead_id,
            signal="high_value_lead",
            gate="outcome",
            score=0.91,
        )
    # Delivery runs off the customer's turn, after the commit (round 7 WP-2, item 5):
    # a test that asserts on it drains it explicitly.
    await escalation_dispatch.drain_background_notifications()
    return written


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


# --- round 7 WP-4b item 7: the ladder and the queue never page the owner twice ------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_rung_that_reaches_the_owner_repoints_the_queued_item_to_the_contact(
    ses, monkeypatch
):
    """Everything refused on the turn: queued (owner included) + ladder scheduled. The
    provider recovers for the OWNER only before the first rung, so the rung's fallback
    lands. The queued item was filed for everyone; left alone, the hourly sweep would
    page the owner a second time for the same escalation. The rung re-points it to the
    configured contact alone, and the replay pages only the contact."""
    monkeypatch.setattr(escalation_dispatch, "ESCALATION_RETRY_DELAYS", (0.25,))
    ses.refuse = None
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid)

    await _fire(admin, iid, sid, lead)
    assert (await _row(admin, sid)).delivery_status == "queued"
    (item,) = await _queued(admin)
    assert item.payload["contacts_only"] is False

    ses.refuse = {CONTACT}  # the owner is reachable again; the contact still is not
    await escalation_dispatch.drain_background_retries()

    assert ses.sent == [OWNER_EMAIL]
    row = await _row(admin, sid)
    assert row.delivery_status == "owner_fallback"
    (item,) = await _queued(admin)
    assert item.payload["contacts_only"] is True
    assert len(await _audit(admin, "escalation_owner_fallback")) == 1

    ses.refuse = set()
    async with tenant_session(admin) as s:
        assert (
            await replay_queued_notification(
                s, item.payload, admin_id=admin, instance_id=iid, resolved_lead_id=lead
            )
            is True
        )
    assert ses.sent == [OWNER_EMAIL, CONTACT]
    assert (await _row(admin, sid)).delivery_status == "delivered"
    assert len(await _audit(admin, "escalation_owner_fallback")) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_replay_stands_down_from_the_owner_when_the_row_already_reads_owner_fallback(
    ses,
):
    """The other half: whatever the queued item says, a row that reads owner_fallback
    means the owner heard. The replay is for the configured contact alone, and while
    the contact stays refused the owner is not paged again."""
    ses.refuse = {CONTACT}
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid)

    await _fire(admin, iid, sid, lead)
    assert ses.sent == [OWNER_EMAIL]
    assert (await _row(admin, sid)).delivery_status == "owner_fallback"
    (item,) = await _queued(admin)

    stale_payload = {**item.payload, "contacts_only": False}  # an item filed before the fix
    async with tenant_session(admin) as s:
        assert (
            await replay_queued_notification(
                s, stale_payload, admin_id=admin, instance_id=iid, resolved_lead_id=lead
            )
            is False
        )
    assert ses.sent == [OWNER_EMAIL]  # the contact was tried and refused; nobody else
    assert (await _row(admin, sid)).delivery_status == "owner_fallback"
    assert len(await _audit(admin, "escalation_owner_fallback")) == 1


# --- round 7 WP-4b item 8: a backlog that outlives a sweep is degraded, and alarms --- #
async def _queue_one_unheard_page(admin: uuid.UUID, iid: uuid.UUID) -> DeferredAction:
    """One escalation nobody could hear: the owner is suppressed and SES refuses the
    contact, so the page lands in the deferred queue."""
    lead = await _seed(admin, iid, owner_suppressed=True)
    await _fire(admin, iid, uuid.uuid4(), lead)
    (item,) = await _queued(admin)
    return item


async def _backdate_queue(admin: uuid.UUID, *, by: timedelta) -> None:
    async with tenant_session(admin) as s:
        await s.execute(
            update(DeferredAction)
            .where(DeferredAction.admin_id == admin)
            .values(created_at=datetime.now(UTC) - by)
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_backlog_carries_the_persisted_moment_it_began(ses):
    """``oldestQueuedAt`` is the queue's own oldest row (migration 0069), read through
    the SECURITY DEFINER function — no process keeps a clock."""
    ses.refuse = None
    admin, iid = uuid.uuid4(), uuid.uuid4()
    item = await _queue_one_unheard_page(admin, iid)

    backlog = await escalation_backlog()
    assert backlog is not None
    assert backlog["queued"] == 1
    assert backlog["oldestQueuedAt"] == item.created_at


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_healthz_degrades_once_the_backlog_outlives_one_sweep_interval(ses):
    """Young, the page is a number outside ``degraded`` (the sweep has not had its
    turn); older than the deferred sweep's cadence it is ``escalation_backlog:1`` IN
    ``degraded`` — the list deploys and operators read."""
    from app.main import create_app

    ses.refuse = None
    admin, iid = uuid.uuid4(), uuid.uuid4()
    await _queue_one_unheard_page(admin, iid)

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://healthz.test") as client:
        young = (await client.get("/healthz")).json()
        assert young["status"] == "ok"
        assert young["escalationBacklog"]["queued"] == 1
        assert not [e for e in young["degraded"] if e.startswith("escalation_backlog:")]

        await _backdate_queue(admin, by=DEFERRED_SWEEP_INTERVAL + timedelta(minutes=5))

        stale = (await client.get("/healthz")).json()
        assert "escalation_backlog:1" in stale["degraded"]
        assert stale["escalationBacklog"]["queued"] == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_deferred_sweep_publishes_the_backlog_it_left_behind(ses, capsys):
    """The EscalationBacklog datum the ObservabilityStack alarm reads is emitted AFTER
    the drain, so it counts what this run could not deliver: SES still refuses, the
    retry fails again, the page stays queued and the datum says 1."""
    from app.jobs import sweeps

    ses.refuse = None
    admin, iid = uuid.uuid4(), uuid.uuid4()
    item = await _queue_one_unheard_page(admin, iid)

    acted = await sweeps.run_deferred_retry(now=item.next_attempt_at + timedelta(seconds=1))

    assert acted == [str(admin)]  # the retry ran for this tenant ...
    (still_queued,) = await _queued(admin)
    assert still_queued.status == "queued" and still_queued.attempts == item.attempts + 1
    data = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if "EscalationBacklog" in line
    ]
    assert data and data[-1]["EscalationBacklog"] == 1.0  # ... and the datum names the miss
    assert data[-1]["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["Environment"]]
