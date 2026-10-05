"""Escalation delivery hardening (round 7 Phase 2 review, dimension ``delivery``).

Each test models the live condition a reviewer found:

* a queued retry whose owner already heard and whose contact has nothing retryable left
  used to be replayed for 24 hours doing nothing, sat in the backlog the whole time and
  ended in a "could not deliver" email to an owner who had heard within a minute — it
  now completes once, as ``settled:owner_heard``, and the row keeps saying
  ``owner_fallback``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.jobs import sweeps
from app.models.audit import AdminAuditLog
from app.models.conversation import EscalationEvent
from app.models.deferred import DeferredAction
from app.models.email_incident import EmailDeliveryIncident
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.lead import Lead
from app.notifications import email_dispatcher, email_notifier, escalation_dispatch
from app.notifications.escalation_dispatch import ESCALATION_RETRY_ACTION, SETTLED_OWNER_HEARD
from app.runtime.integration_status import escalation_backlog
from app.runtime.orchestrator import _record_escalation
from sqlalchemy import select, update

pytestmark = [pytest.mark.asyncio]

OWNER_EMAIL = "owner@acme.example"
CONTACT = "sales@acme.example"


class SelectiveEmailSender:
    """SES as it misbehaves: refuses some recipients, or everyone, until told otherwise.
    Bound for BOTH the escalation notifier and the platform notice dispatcher, so an
    abandon notice ("could not deliver ...") is seen here too."""

    def __init__(self) -> None:
        self.refuse: set[str] | None = set()  # None → refuse everyone
        self.sent: list[dict[str, str]] = []

    async def send(self, *, to, subject, body_text, from_address=None, tags=None):
        if self.refuse is None or to in self.refuse:
            raise RuntimeError("throttled")
        self.sent.append({"to": to, "subject": subject, "body": body_text})
        return f"ses-{len(self.sent)}"

    @property
    def recipients(self) -> list[str]:
        return [m["to"] for m in self.sent]


@pytest.fixture
def ses(monkeypatch) -> SelectiveEmailSender:
    fake = SelectiveEmailSender()
    monkeypatch.setattr(email_notifier, "build_email_sender", lambda: fake)
    monkeypatch.setattr(email_dispatcher, "build_email_sender", lambda: fake)
    return fake


async def _seed(
    admin: uuid.UUID,
    iid: uuid.UUID,
    *,
    escalation: dict[str, object],
    verified_emails: tuple[str, ...] = (),
) -> uuid.UUID:
    lead_id = uuid.uuid4()
    async with tenant_session(admin) as s:
        for address in verified_emails:
            s.add(
                EmailDeliveryIncident(
                    admin_id=admin, email_lower=address, verified_at=datetime.now(UTC)
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
                escalation=escalation,
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


async def _fire(admin: uuid.UUID, iid: uuid.UUID, sid: uuid.UUID, lead_id: uuid.UUID) -> None:
    """The runtime's own write + the off-turn notification it schedules, drained."""
    async with tenant_session(admin) as s:
        await _record_escalation(
            s,
            admin_id=admin,
            instance_id=iid,
            session_id=sid,
            resolved_lead_id=lead_id,
            signal="high_value_lead",
            gate="outcome",
            score=0.91,
        )
    await escalation_dispatch.drain_background_notifications()


async def _row(admin: uuid.UUID, sid: uuid.UUID) -> EscalationEvent:
    async with tenant_session(admin) as s:
        return (
            await s.execute(
                select(EscalationEvent).where(
                    EscalationEvent.admin_id == admin, EscalationEvent.session_id == sid
                )
            )
        ).scalar_one()


async def _items(admin: uuid.UUID) -> list[DeferredAction]:
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


async def _abandons(admin: uuid.UUID) -> list[AdminAuditLog]:
    return [
        e
        for e in await _audit(admin, "escalation_delivery_failed")
        if e.event_payload.get("action") == "deferred_abandoned"
    ]


def _backlog_data(captured: str) -> list[float]:
    return [
        json.loads(line)["EscalationBacklog"]
        for line in captured.splitlines()
        if "EscalationBacklog" in line
    ]


# --- a stand-down completes its queue item, once, honestly ---------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_owner_blip_healed_by_the_ladder_completes_the_queued_item(
    ses, monkeypatch, capsys
):
    """The default tenant after signup has no escalation contact. SES throttles the
    owner email once, so the page is queued; the ladder's rung delivers it half a
    minute later. The queued item has nothing left to do: it is completed right there —
    not replayed hourly for 24h, counted in the backlog, and ended with an email
    telling the owner (who heard) that the alert could not be delivered."""
    monkeypatch.setattr(escalation_dispatch, "ESCALATION_RETRY_DELAYS", (0.25,))
    ses.refuse = None
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid, escalation={})

    await _fire(admin, iid, sid, lead)
    assert (await _row(admin, sid)).delivery_status == "queued"
    (item,) = await _items(admin)
    assert item.status == "queued"

    ses.refuse = set()  # the blip clears before the first rung
    await escalation_dispatch.drain_background_retries()

    assert ses.recipients == [OWNER_EMAIL]
    row = await _row(admin, sid)
    assert row.delivery_status == "owner_fallback"  # the owner heard; never "delivered"
    (item,) = await _items(admin)
    assert item.status == "delivered"
    assert item.last_error == SETTLED_OWNER_HEARD
    backlog = await escalation_backlog()
    assert backlog is not None and backlog["queued"] == 0

    # The hourly sweep, the next hour and again past the item's TTL: nothing to replay,
    # nothing to abandon, nobody mailed, no backlog datum above zero.
    capsys.readouterr()
    for now in (datetime.now(UTC) + timedelta(hours=1), item.expires_at + timedelta(minutes=5)):
        assert await sweeps.run_deferred_retry(now=now) == []
    assert ses.recipients == [OWNER_EMAIL]
    assert await _abandons(admin) == []
    assert (await _row(admin, sid)).delivery_status == "owner_fallback"
    assert set(_backlog_data(capsys.readouterr().out)) == {0.0}
    assert len(await _audit(admin, "escalation_owner_fallback")) == 1


@pytest.mark.parametrize("contact", [None, "not-an-address"])
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_replay_with_nothing_left_to_retry_stands_down_and_completes_once(ses, contact):
    """The same zombie reached through the sweep: the item is still queued (its ladder
    died with the process, or it was filed before the rung settled items) while the row
    already reads ``owner_fallback``, and the configured contact offers nothing a retry
    could change — none at all, or one that can never be an address. The replay stands
    down from the owner, completes the item exactly once and pages nobody."""
    ses.refuse = None
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid, escalation={"primaryEmail": contact} if contact else {})

    await _fire(admin, iid, sid, lead)
    (item,) = await _items(admin)
    assert item.status == "queued" and item.payload["contacts_only"] is False
    # The owner heard through a pass that left the item queued.
    async with tenant_session(admin) as s:
        await s.execute(
            update(EscalationEvent)
            .where(EscalationEvent.admin_id == admin, EscalationEvent.session_id == sid)
            .values(delivery_status="owner_fallback", delivery_detail="no_contact_configured")
        )

    ses.refuse = set()
    acted = await sweeps.run_deferred_retry(now=item.next_attempt_at + timedelta(seconds=1))

    assert acted == [str(admin)]
    (item,) = await _items(admin)
    assert item.status == "delivered"
    assert item.last_error == SETTLED_OWNER_HEARD
    assert item.attempts == 0  # completed, not retried-and-backed-off
    assert ses.recipients == []  # the owner is not paged a second time
    assert (await _row(admin, sid)).delivery_status == "owner_fallback"

    # Past the TTL the old code abandoned it: a terminal "delivery failed" audit row and
    # the "could not deliver an escalation alert" email. Neither happens now.
    assert await sweeps.run_deferred_retry(now=item.expires_at + timedelta(minutes=5)) == []
    assert await _abandons(admin) == []
    assert ses.recipients == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_contact_that_can_still_recover_keeps_its_retry(ses):
    """The stand-down is only for an item with nothing retryable: a contact the
    provider refused a moment ago is still tried on every pass, and its item stays
    queued until it lands or ages out."""
    ses.refuse = {CONTACT}
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead = await _seed(admin, iid, escalation={"primaryEmail": CONTACT}, verified_emails=(CONTACT,))

    await _fire(admin, iid, sid, lead)
    assert ses.recipients == [OWNER_EMAIL]
    (item,) = await _items(admin)
    assert item.payload["contacts_only"] is True

    await sweeps.run_deferred_retry(now=item.next_attempt_at + timedelta(seconds=1))
    (item,) = await _items(admin)
    assert item.status == "queued" and item.attempts == 1
    assert item.last_error == "delivery_failed"

    ses.refuse = set()
    await sweeps.run_deferred_retry(now=item.next_attempt_at + timedelta(seconds=1))
    (item,) = await _items(admin)
    assert item.status == "delivered"
    assert ses.recipients == [OWNER_EMAIL, CONTACT]
    assert (await _row(admin, sid)).delivery_status == "delivered"
