"""A refused escalation notification is retried, not just audited (P1-6, §3.5.4/§3.8.7 D).

CJ Phase 6 promises the owner hears about a hot lead without logging in. A transient SES
or Twilio blip used to end that story at one ``escalation_delivery_failed`` row: the
escalation record was intact, the audit was loud, and nobody was told. These tests pin the
retry half — that a failure which could plausibly clear on its own is handed to the rule-D
queue, that replaying it never forks a second 24h TTL, and that the terminal miss at the
TTL is filed under the event name the escalation surface already means by "the owner was
never told".

``test_escalation_delivery.py`` owns routing and the first-attempt audit trail; nothing
here re-tests those.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.connections import deferred_queue
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.deferred import DeferredAction
from app.models.instance import Account, AccountState, Instance
from app.notifications import email_notifier
from app.notifications.escalation_dispatch import ESCALATION_RETRY_ACTION, notify_escalation
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

# ``notify_escalation`` enqueues on the wall clock, so the sweep clock is derived from
# it rather than pinned to a literal date that would never be due.
SETTLED = timedelta(seconds=5)
OWNER_EMAIL = "owner@acme.example"
CONTACT = "sales@acme.example"


class FlakyEmailSender:
    """SES as it actually misbehaves: it raises, and then later it doesn't."""

    def __init__(self) -> None:
        self.up = False
        self.sent: list[str] = []

    async def send(self, *, to, subject, body_text, from_address=None, tags=None):
        if not self.up:
            raise RuntimeError("throttled")
        self.sent.append(to)
        return f"ses-{len(self.sent)}"


@pytest.fixture
def ses(monkeypatch) -> FlakyEmailSender:
    fake = FlakyEmailSender()
    monkeypatch.setattr(email_notifier, "build_email_sender", lambda: fake)
    return fake


async def _seed(
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    escalation: dict[str, object],
    with_account: bool = True,
    verified_contacts: tuple[str, ...] = (),
) -> None:
    async with tenant_session(admin_id) as s:
        for address in verified_contacts:
            # Round 5B item 13: a delivered-but-UNVERIFIED contact additionally
            # copies the owner; tests pinning exact recipient lists confirm the
            # contact first (the copy behavior has its own contract tests).
            from app.models.email_incident import EmailDeliveryIncident  # noqa: PLC0415

            s.add(
                EmailDeliveryIncident(
                    admin_id=admin_id,
                    email_lower=address.strip().lower(),
                    verified_at=datetime.now(UTC),
                )
            )
        if with_account:
            s.add(
                Account(
                    id=admin_id,
                    admin_id=admin_id,
                    user_id=uuid.uuid4(),
                    email=OWNER_EMAIL,
                    email_lower=OWNER_EMAIL,
                    password_hash="x",
                    state=AccountState.verified,
                    email_verified=True,
                )
            )
        s.add(
            Instance(id=instance_id, admin_id=admin_id, name="Acme Patios", escalation=escalation)
        )


async def _fire(admin_id: uuid.UUID, instance_id: uuid.UUID) -> bool:
    async with tenant_session(admin_id) as s:
        return await notify_escalation(
            s,
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=uuid.uuid4(),
            resolved_lead_id=None,
            signal="high_value_lead",
            gate="outcome",
            score=0.91,
        )


async def _queued(admin_id: uuid.UUID) -> list[DeferredAction]:
    async with tenant_session(admin_id) as s:
        return list((await s.execute(select(DeferredAction))).scalars().all())


async def _sweep(admin_id: uuid.UUID, *, now: datetime) -> dict[str, int]:
    async with tenant_session(admin_id) as s:
        return await deferred_queue.run_sweep(
            s, admin_id=admin_id, deliverer=deferred_queue.ChannelDeliverer(s), now=now
        )


async def _events(admin_id: uuid.UUID, event_type: str) -> list[AdminAuditLog]:
    async with tenant_session(admin_id) as s:
        return [
            r
            for r in (await s.execute(select(AdminAuditLog).order_by(AdminAuditLog.seq.asc())))
            .scalars()
            .all()
            if r.event_type == event_type
        ]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_provider_blip_queues_the_escalation_instead_of_ending_at_an_audit_row(ses):
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    await _seed(admin_id, instance_id, escalation={"primaryEmail": CONTACT})

    assert await _fire(admin_id, instance_id) is False

    (queued,) = await _queued(admin_id)
    assert queued.action_type == ESCALATION_RETRY_ACTION
    assert queued.status == "queued"
    # No instance_connections row describes the platform's own sender, and gating on an
    # absent row would hold the item at attempt 0 until the TTL abandoned it.
    assert queued.connection_type == deferred_queue.PLATFORM_NOTIFICATION
    assert queued.payload["signal"] == "high_value_lead"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_owner_fallback_being_refused_counts_as_a_refusal_not_an_absence(ses):
    """A tenant who configured no escalation contacts still has an owner email, so a SES
    blip there is the same transient failure — not the ``no_contact_configured`` gap."""
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    await _seed(admin_id, instance_id, escalation={})

    await _fire(admin_id, instance_id)

    assert len(await _queued(admin_id)) == 1
    assert await _events(admin_id, "escalation_owner_fallback") == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_escalation_with_nowhere_at_all_to_send_is_reported_once_and_not_queued(ses):
    """An empty config with no owner address will look exactly the same tomorrow.
    Retrying it for 24h would bury the recoverable failures under noise."""
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    await _seed(admin_id, instance_id, escalation={}, with_account=False)

    await _fire(admin_id, instance_id)

    assert await _queued(admin_id) == []
    (failed,) = await _events(admin_id, "escalation_delivery_failed")
    assert failed.event_payload["failure_reason"] == "no_contact_configured"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_replay_that_fails_again_backs_off_rather_than_forking_a_new_ttl(ses):
    """If the replay re-enqueued on failure, every sweep would start a fresh 24h TTL and
    the item would never abandon — a retry loop with no end is not a fail-safe."""
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    await _seed(admin_id, instance_id, escalation={"primaryEmail": CONTACT})
    await _fire(admin_id, instance_id)
    (before,) = await _queued(admin_id)

    swept_at = datetime.now(UTC) + SETTLED
    assert await _sweep(admin_id, now=swept_at) == {
        "delivered": 0,
        "abandoned": 0,
        "retried": 1,
        "held": 0,
    }

    (after,) = await _queued(admin_id)
    assert after.id == before.id
    assert after.attempts == 1
    assert after.expires_at == before.expires_at
    assert after.next_attempt_at == swept_at + timedelta(minutes=2)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_owner_is_paged_once_the_provider_recovers(ses):
    """The whole point: the admin hears about the lead late instead of never."""
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    await _seed(
        admin_id,
        instance_id,
        escalation={"primaryEmail": CONTACT},
        verified_contacts=(CONTACT,),
    )
    await _fire(admin_id, instance_id)

    ses.up = True
    assert (await _sweep(admin_id, now=datetime.now(UTC) + SETTLED))["delivered"] == 1

    assert ses.sent == [CONTACT]
    (row,) = await _queued(admin_id)
    assert row.status == "delivered"
    assert len(await _events(admin_id, "escalation_notification_sent")) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_notification_abandoned_at_the_ttl_is_filed_as_a_delivery_failure(ses):
    """24h of refusals ends in the terminal event the escalation surface reads, not in a
    generic connection event only someone who already knew to look would find."""
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    await _seed(admin_id, instance_id, escalation={"primaryEmail": CONTACT})
    await _fire(admin_id, instance_id)

    assert (await _sweep(admin_id, now=datetime.now(UTC) + timedelta(hours=25)))["abandoned"] == 1

    (row,) = await _queued(admin_id)
    assert row.status == "abandoned"
    assert row.last_error == "ttl_exceeded"
    abandoned = [
        e
        for e in await _events(admin_id, "escalation_delivery_failed")
        if e.event_payload.get("action") == "deferred_abandoned"
    ]
    assert len(abandoned) == 1
    assert abandoned[0].event_payload["action_type"] == ESCALATION_RETRY_ACTION
