"""Escalation notification DELIVERY (Architecture §3.5.3 / §3.5.4 / §3.5.5).

CJ Phase 6 promises the owner hears about a hot lead without logging in. These tests pin
that promise end-to-end from the fired signal: the right contact is reached on the right
channel, the cc-owner copy goes out, and a channel that cannot deliver says so loudly in
the audit log instead of dropping the escalation on the floor.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.audit import log as audit
from app.connections.secrets import secret_ref_for
from app.db.session import tenant_session
from app.models.audit import ActorType, AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.conversation import EscalationEvent
from app.models.deferred import DeferredAction
from app.models.email_incident import EmailDeliveryIncident, EscalationContactConfirmation
from app.models.instance import Account, AccountState, Instance
from app.models.lead import Lead
from app.notifications import email_notifier, sms_notifier
from app.notifications.base import EscalationNotification
from app.notifications.escalation_dispatch import notify_escalation
from app.runtime.orchestrator import ASK_ATTEMPT_BOUND
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

OWNER_EMAIL = "owner@acme.example"


class FakeEmailSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, str | None]] = []

    async def send(self, *, to, subject, body_text, from_address=None, tags=None):
        self.sent.append(
            {"to": to, "subject": subject, "body": body_text, "from_address": from_address}
        )
        return f"ses-{len(self.sent)}"


class FakeSmsSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send_sms(self, from_number, to, body):
        self.sent.append({"from_number": from_number, "to": to, "body": body})
        return f"SM-{len(self.sent)}"


@pytest.fixture
def senders(monkeypatch) -> tuple[FakeEmailSender, FakeSmsSender]:
    """Bind both notification transports to in-memory fakes (no network, no AWS)."""
    email_fake, sms_fake = FakeEmailSender(), FakeSmsSender()
    monkeypatch.setattr(email_notifier, "build_email_sender", lambda: email_fake)

    async def _tenant_sender(*a, **kw):
        return sms_fake

    monkeypatch.setattr(sms_notifier, "build_tenant_sms_sender", _tenant_sender)
    return email_fake, sms_fake


async def _seed_tenant(
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    escalation: dict[str, object],
    sms_number: str | None = None,
    verified_contacts: tuple[str, ...] = (),
    verified_sms_contacts: tuple[str, ...] = (),
    lead_contact: str = "dana@lead.example",
    lead_email: str | None = None,
    lead_phone: str | None = None,
    owner_suppressed: bool = False,
) -> uuid.UUID:
    """One verified account + one Luciel with the given escalation pillar, plus a lead.

    ``verified_contacts`` marks escalation email addresses as CONFIRMED (round 5B
    item 13): a delivered-but-unverified contact additionally copies the account
    owner, so routing tests that pin exact recipient lists seed the confirmation.
    The unverified-copy behavior itself is pinned in
    ``tests/contract/test_escalation_contact_reliability.py``. ``verified_sms_contacts``
    is the same for a number (round 7 WP-4b): a number that never answered its
    confirmation code copies the owner too — pinned below.
    """
    lead_id = uuid.uuid4()
    async with tenant_session(admin_id) as s:
        for address in verified_contacts:
            s.add(
                EmailDeliveryIncident(
                    admin_id=admin_id,
                    email_lower=address.strip().lower(),
                    verified_at=datetime.now(UTC),
                )
            )
        for number in verified_sms_contacts:
            s.add(
                EscalationContactConfirmation(
                    admin_id=admin_id,
                    kind="sms",
                    value_normalized=number,
                    confirmed_at=datetime.now(UTC),
                )
            )
        if owner_suppressed:
            s.add(
                EmailDeliveryIncident(
                    admin_id=admin_id, email_lower=OWNER_EMAIL, hard_bounced_at=datetime.now(UTC)
                )
            )
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
            Instance(
                id=instance_id,
                admin_id=admin_id,
                name="Acme Patios",
                escalation=escalation,
            )
        )
        s.add(
            Lead(
                id=lead_id,
                admin_id=admin_id,
                instance_id=instance_id,
                name="Dana Lee",
                contact_identifier=lead_contact,
                email=lead_email,
                phone=lead_phone,
                intent="Wants a quote for a 20x20 patio",
                lead_score=0.91,
            )
        )
        if sms_number:
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=admin_id,
                    instance_id=instance_id,
                    connection_type="sms_sender",
                    auth_class="api_key",
                    provider="twilio",
                    status="connected",
                    secret_ref=secret_ref_for(admin_id, instance_id, "sms_sender"),
                    non_secret_config={"destination": sms_number},
                )
            )
        await s.flush()
    return lead_id


async def _fire(
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    lead_id: uuid.UUID | None,
    *,
    signal: str = "high_value_lead",
    gate: str = "outcome",
    session_id: uuid.UUID | None = None,
) -> list[AdminAuditLog]:
    async with tenant_session(admin_id) as s:
        await notify_escalation(
            s,
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=session_id or uuid.uuid4(),
            resolved_lead_id=lead_id,
            signal=signal,
            gate=gate,
            score=0.91,
        )
        await s.flush()
        return list(
            (
                await s.execute(
                    select(AdminAuditLog)
                    .where(AdminAuditLog.admin_id == admin_id)
                    .order_by(AdminAuditLog.seq.asc())
                )
            )
            .scalars()
            .all()
        )


def _events(rows: list[AdminAuditLog], event_type: str) -> list[AdminAuditLog]:
    return [r for r in rows if r.event_type == event_type]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_escalation_notifies_the_primary_contact(senders):
    """The base promise: a fired escalation reaches the configured primary contact."""
    email_fake, _ = senders
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    lead_id = await _seed_tenant(
        admin_id,
        instance_id,
        escalation={"primaryEmail": "sales@acme.example"},
        verified_contacts=("sales@acme.example",),
    )

    rows = await _fire(admin_id, instance_id, lead_id)

    assert [m["to"] for m in email_fake.sent] == ["sales@acme.example"]
    # §3.5.1: actionable without logging in — the lead's contact info is in the body.
    assert "dana@lead.example" in str(email_fake.sent[0]["body"])
    sent = _events(rows, "escalation_notification_sent")
    assert len(sent) == 1
    assert sent[0].event_payload["recipient_role"] == "primary"
    assert sent[0].event_payload["delivery_method"] == "email"
    # §5.2/§3.4.11: the audit payload references the session, never the lead's contact.
    assert "dana@lead.example" not in str(sent[0].event_payload)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_preferred_channel_sms_pages_the_sms_contact_from_the_tenant_number(senders):
    """preferredChannel=sms must page by SMS — and from the tenant's OWN number (§3.1.4)."""
    email_fake, sms_fake = senders
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    lead_id = await _seed_tenant(
        admin_id,
        instance_id,
        escalation={
            "primaryEmail": "sales@acme.example",
            "primarySms": "+15550001111",
            "preferredChannel": "sms",
        },
        sms_number="+15559990000",
        verified_sms_contacts=("+15550001111",),
    )

    rows = await _fire(admin_id, instance_id, lead_id)

    assert sms_fake.sent == [
        {
            "from_number": "+15559990000",
            "to": "+15550001111",
            "body": sms_fake.sent[0]["body"],
        }
    ]
    assert "dana@lead.example" in sms_fake.sent[0]["body"]
    assert email_fake.sent == []  # the email contact must NOT also be paged
    assert (
        _events(rows, "escalation_notification_sent")[0].event_payload["delivery_method"] == "sms"
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_cc_owner_email_fans_out_alongside_a_successful_primary(senders):
    """ccOwnerEmail is fan-out, not fallback: "page by SMS AND cc the owner" (§3.5.3)."""
    email_fake, sms_fake = senders
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    lead_id = await _seed_tenant(
        admin_id,
        instance_id,
        escalation={
            "primarySms": "+15550001111",
            "routing": [
                {"signal": "high_value_lead", "channel": "sms", "ccOwnerEmail": True},
            ],
        },
        sms_number="+15559990000",
    )

    rows = await _fire(admin_id, instance_id, lead_id)

    assert len(sms_fake.sent) == 1
    assert [m["to"] for m in email_fake.sent] == [OWNER_EMAIL]
    roles = {
        r.event_payload["recipient_role"] for r in _events(rows, "escalation_notification_sent")
    }
    assert roles == {"primary", "cc_owner_email"}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_secondary_contact_takes_over_when_the_primary_send_fails(senders):
    """The secondary contact is the fallback when the primary cannot be reached (§3.5.3)."""
    email_fake, _ = senders
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    lead_id = await _seed_tenant(
        admin_id,
        instance_id,
        escalation={
            # A phone number in the email field can never be delivered as email.
            "primaryEmail": "+15550001111",
            "secondaryEmail": "backup@acme.example",
        },
        verified_contacts=("backup@acme.example",),
    )

    rows = await _fire(admin_id, instance_id, lead_id)

    assert [m["to"] for m in email_fake.sent] == ["backup@acme.example"]
    failed = _events(rows, "escalation_delivery_failed")
    assert len(failed) == 1
    assert failed[0].event_payload["recipient_role"] == "primary"
    assert failed[0].event_payload["failure_reason"] == "malformed_contact"
    assert (
        _events(rows, "escalation_notification_sent")[0].event_payload["recipient_role"]
        == "secondary"
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_unprovisioned_sms_channel_degrades_to_the_owner_email_not_silence(senders):
    """The actionable-degrade case: the admin chose SMS but never provisioned the SMS
    channel. The escalation must still reach a human, and the reason must be recorded —
    a silent drop is the failure mode CJ Phase 6 cannot tolerate."""
    email_fake, sms_fake = senders
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    lead_id = await _seed_tenant(
        admin_id,
        instance_id,
        escalation={"primarySms": "+15550001111", "preferredChannel": "sms"},
        sms_number=None,  # no sms_sender connection
    )

    rows = await _fire(admin_id, instance_id, lead_id)

    assert sms_fake.sent == []
    failed = _events(rows, "escalation_delivery_failed")
    assert len(failed) == 1
    assert failed[0].event_payload["failure_reason"] == "sms_channel_not_provisioned"
    # Fell back to the account owner's email, and said so.
    assert [m["to"] for m in email_fake.sent] == [OWNER_EMAIL]
    fallback = _events(rows, "escalation_owner_fallback")
    assert len(fallback) == 1
    assert fallback[0].event_payload["configured_channel"] == "sms"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_no_configured_contact_still_reaches_the_account_owner(senders):
    """An admin who never configured escalation contacts still gets the hot lead."""
    email_fake, _ = senders
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    lead_id = await _seed_tenant(admin_id, instance_id, escalation={})

    rows = await _fire(admin_id, instance_id, lead_id)

    assert [m["to"] for m in email_fake.sent] == [OWNER_EMAIL]
    fallback = _events(rows, "escalation_owner_fallback")
    assert fallback[0].event_payload["reason"] == "no_contact_configured"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_server_only_signal_with_no_lead_still_notifies(senders):
    """budget_exhausted / llm_unavailable fire without a resolved lead — the owner still
    needs to know, and the body must not render as a broken template."""
    email_fake, _ = senders
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant(admin_id, instance_id, escalation={"primaryEmail": "sales@acme.example"})

    await _fire(admin_id, instance_id, None, signal="budget_exhausted")

    body = str(email_fake.sent[0]["body"])
    assert "not captured" in body
    assert "payment method" in body


# --- round 7 WP-4b: an SMS contact is proven, or the owner is copied ----------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_unconfirmed_sms_contact_pages_the_number_and_copies_the_owner(senders):
    """Twilio accepting a request used to be the whole proof that a number could hear a
    page. Until the contact answers its confirmation code, every page to it also goes
    to the account owner — the rule the email loop already had (round 5B item 13)."""
    email_fake, sms_fake = senders
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    lead_id = await _seed_tenant(
        admin_id,
        instance_id,
        escalation={"primarySms": "+15550001111", "preferredChannel": "sms"},
        sms_number="+15559990000",
    )

    rows = await _fire(admin_id, instance_id, lead_id)

    assert [m["to"] for m in sms_fake.sent] == ["+15550001111"]
    assert [m["to"] for m in email_fake.sent] == [OWNER_EMAIL]
    roles = [
        r.event_payload["recipient_role"] for r in _events(rows, "escalation_notification_sent")
    ]
    assert roles == ["primary", "owner_unverified_copy"]
    fallback = _events(rows, "escalation_owner_fallback")
    assert len(fallback) == 1
    assert fallback[0].event_payload["reason"] == "contact_unverified"
    assert fallback[0].event_payload["configured_channel"] == "sms"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_confirmed_sms_contact_is_paged_alone(senders):
    """Once the number answered its code, the owner is not copied any more."""
    email_fake, sms_fake = senders
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    lead_id = await _seed_tenant(
        admin_id,
        instance_id,
        escalation={"primarySms": "+15550001111", "preferredChannel": "sms"},
        sms_number="+15559990000",
        verified_sms_contacts=("+15550001111",),
    )

    rows = await _fire(admin_id, instance_id, lead_id)

    assert [m["to"] for m in sms_fake.sent] == ["+15550001111"]
    assert email_fake.sent == []
    assert _events(rows, "escalation_owner_fallback") == []


# --- round 7 WP-4b: the payload carries what the lead volunteered (item 3) ----------- #
async def _escalation_row(
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    lead_id: uuid.UUID | None,
    *,
    signal: str = "high_value_lead",
    gate: str = "outcome",
    reasons: list[str] | None = None,
) -> uuid.UUID:
    """The row the runtime writes before delivery runs — what ``delivery_status`` and
    the send-time reasons are read from."""
    session_id = uuid.uuid4()
    async with tenant_session(admin_id) as s:
        s.add(
            EscalationEvent(
                admin_id=admin_id,
                instance_id=instance_id,
                session_id=session_id,
                resolved_lead_id=lead_id,
                signal=signal,
                gate=gate,
                score_or_confidence=0.91,
                reasons=reasons,
            )
        )
        await s.flush()
    return session_id


async def _row(admin_id: uuid.UUID, session_id: uuid.UUID) -> EscalationEvent:
    async with tenant_session(admin_id) as s:
        return (
            await s.execute(
                select(EscalationEvent).where(
                    EscalationEvent.admin_id == admin_id,
                    EscalationEvent.session_id == session_id,
                )
            )
        ).scalar_one()


async def _queued_count(admin_id: uuid.UUID) -> int:
    async with tenant_session(admin_id) as s:
        return len(
            (
                await s.execute(
                    select(DeferredAction).where(
                        DeferredAction.admin_id == admin_id,
                        DeferredAction.action_type == "escalation_notification",
                    )
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_page_carries_the_email_and_phone_the_lead_volunteered(senders):
    """An SMS caller who said "email me at dana@..." used to be paged with the number
    alone: the notification was built from contact_identifier only, and the address
    the lead volunteered (lead.email, round 5A) never reached the owner."""
    email_fake, sms_fake = senders
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    lead_id = await _seed_tenant(
        admin_id,
        instance_id,
        escalation={
            "primaryEmail": "sales@acme.example",
            "primarySms": "+15550001111",
            "routing": [
                {"signal": "high_value_lead", "channel": "sms", "ccOwnerEmail": True},
            ],
        },
        sms_number="+15559990000",
        verified_contacts=("sales@acme.example",),
        verified_sms_contacts=("+15550001111",),
        lead_contact="+16045550123",
        lead_email="dana@lead.example",
    )

    await _fire(admin_id, instance_id, lead_id)

    (page,) = sms_fake.sent
    assert "+16045550123" in page["body"] and "dana@lead.example" in page["body"]
    (copy,) = email_fake.sent
    assert "Contact: +16045550123" in str(copy["body"])
    assert "Email: dana@lead.example" in str(copy["body"])
    assert "Phone:" not in str(copy["body"])  # nothing volunteered, nothing invented


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_volunteered_phone_on_an_email_keyed_lead_is_carried_too(senders):
    email_fake, _ = senders
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    lead_id = await _seed_tenant(
        admin_id,
        instance_id,
        escalation={"primaryEmail": "sales@acme.example"},
        verified_contacts=("sales@acme.example",),
        lead_phone="+16045550999",
    )

    await _fire(admin_id, instance_id, lead_id)

    (page,) = email_fake.sent
    assert "Contact: dana@lead.example" in str(page["body"])
    assert "Phone: +16045550999" in str(page["body"])
    assert "Email:" not in str(page["body"])  # the contact already IS the email


# --- round 7 WP-4b: a moderation take-over is a safety page (item 4) ---------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_moderation_take_over_is_paged_as_a_safety_page_not_a_request_for_a_person(
    senders,
):
    """D2 routes threats and self-harm through the explicit_human_request contact;
    the row carries no reason token, only the ``content_moderation_event`` the
    runtime audited for the session. The page must say SAFETY, never that the
    customer asked to speak to a person."""
    email_fake, _ = senders
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    lead_id = await _seed_tenant(
        admin_id,
        instance_id,
        escalation={"primaryEmail": "sales@acme.example"},
        verified_contacts=("sales@acme.example",),
    )
    session_id = await _escalation_row(
        admin_id, instance_id, lead_id, signal="explicit_human_request", gate="intake"
    )
    async with tenant_session(admin_id) as s:
        await audit.emit(
            s,
            admin_id=admin_id,
            instance_id=instance_id,
            event_type="content_moderation_event",
            actor_type=ActorType.luciel_runtime,
            payload={"category": "self_harm", "session_id": str(session_id)},
        )

    await _fire(
        admin_id,
        instance_id,
        lead_id,
        signal="explicit_human_request",
        gate="intake",
        session_id=session_id,
    )

    (page,) = email_fake.sent
    assert str(page["subject"]).startswith("SAFETY: a customer may be at risk of self-harm")
    body = str(page["body"])
    assert "Why: SAFETY" in body and "self-harm" in body
    assert "asked" not in body.lower() and "speak to a person" not in body.lower()
    assert "emergency services" in body


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_real_request_for_a_person_is_still_rendered_as_one(senders):
    email_fake, _ = senders
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    lead_id = await _seed_tenant(
        admin_id,
        instance_id,
        escalation={"primaryEmail": "sales@acme.example"},
        verified_contacts=("sales@acme.example",),
    )
    session_id = await _escalation_row(
        admin_id,
        instance_id,
        lead_id,
        signal="explicit_human_request",
        gate="intake",
        reasons=["the customer asked for a person"],
    )

    await _fire(
        admin_id,
        instance_id,
        lead_id,
        signal="explicit_human_request",
        gate="intake",
        session_id=session_id,
    )

    (page,) = email_fake.sent
    assert "SAFETY" not in str(page["subject"]) and "SAFETY" not in str(page["body"])
    assert "Details: the customer asked for a person" in str(page["body"])


# --- round 7 WP-4b: terminal refusals are not retried for 24h (item 6) -------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_malformed_email_contact_is_stamped_now_and_surfaces_as_bouncing(senders):
    """A contact that could never be an address looks exactly the same tomorrow: the
    owner hears once (fallback), the row is settled, nothing is queued for 24 hours,
    and the address is filed as a delivery incident, the seam the dashboard's
    ``escalation_bouncing`` need and the contact-health surface already read."""
    email_fake, _ = senders
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    lead_id = await _seed_tenant(
        admin_id, instance_id, escalation={"primaryEmail": "not an address"}
    )
    session_id = await _escalation_row(admin_id, instance_id, lead_id)

    rows = await _fire(admin_id, instance_id, lead_id, session_id=session_id)

    assert [m["to"] for m in email_fake.sent] == [OWNER_EMAIL]
    row = await _row(admin_id, session_id)
    assert row.delivery_status == "owner_fallback"
    assert row.delivery_detail == "malformed_contact"
    assert await _queued_count(admin_id) == 0
    failed = _events(rows, "escalation_delivery_failed")
    assert [e.event_payload["failure_reason"] for e in failed] == ["malformed_contact"]
    async with tenant_session(admin_id) as s:
        incident = (
            await s.execute(
                select(EmailDeliveryIncident).where(
                    EmailDeliveryIncident.admin_id == admin_id,
                    EmailDeliveryIncident.email_lower == "not an address",
                )
            )
        ).scalar_one()
    assert incident.hard_bounced_at is not None
    assert incident.last_event_detail == "malformed_contact"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_sms_contact_with_no_number_to_send_from_is_failed_not_queued(senders):
    """sms_channel_not_provisioned cannot clear on its own; with the owner's own
    address suppressed there is nobody left to tell: the row reads ``failed`` now,
    with the reason, instead of a day of retries ending in the same answer."""
    email_fake, sms_fake = senders
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    lead_id = await _seed_tenant(
        admin_id,
        instance_id,
        escalation={"primarySms": "+15550001111", "preferredChannel": "sms"},
        owner_suppressed=True,
    )
    session_id = await _escalation_row(admin_id, instance_id, lead_id)

    rows = await _fire(admin_id, instance_id, lead_id, session_id=session_id)

    assert sms_fake.sent == [] and email_fake.sent == []
    row = await _row(admin_id, session_id)
    assert row.delivery_status == "failed"
    assert row.delivery_detail == "sms_channel_not_provisioned"
    assert await _queued_count(admin_id) == 0
    reasons = [
        e.event_payload["failure_reason"] for e in _events(rows, "escalation_delivery_failed")
    ]
    assert reasons == ["sms_channel_not_provisioned", "owner_email_suppressed"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_provider_blip_is_still_queued(senders, monkeypatch):
    """The contrast: a refusal that CAN clear keeps the rule-D queue."""
    email_fake, _ = senders
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    lead_id = await _seed_tenant(
        admin_id,
        instance_id,
        escalation={"primaryEmail": "sales@acme.example"},
        verified_contacts=("sales@acme.example",),
        owner_suppressed=True,
    )
    session_id = await _escalation_row(admin_id, instance_id, lead_id)

    async def _throttled(**kwargs):
        raise RuntimeError("throttled")

    monkeypatch.setattr(email_fake, "send", _throttled)
    await _fire(admin_id, instance_id, lead_id, session_id=session_id)

    assert (await _row(admin_id, session_id)).delivery_status == "queued"
    assert await _queued_count(admin_id) == 1


# --- round 7 WP-4b item 4: the renderings the read-back above does not pin ---------- #
#
# The moderation take-over can name its category on the row itself (first reason
# ``moderation:<category>``) instead of only in the audit trail; a flagged row says WHAT
# was flagged; the ask bound lists the field names still missing. Each is rendered on
# both channels — the SMS is the one the owner reads on the move.
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_threat_named_in_the_rows_first_reason_is_a_safety_page(senders):
    """Reasons path, end to end: the row's ``moderation:threats_violence`` token routes
    the page where a request for a person goes (the signal is still
    explicit_human_request) and renders it as SAFETY. The token never reaches the
    owner; the phrase behind it does."""
    email_fake, _ = senders
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    lead_id = await _seed_tenant(
        admin_id,
        instance_id,
        escalation={"primaryEmail": "sales@acme.example"},
        verified_contacts=("sales@acme.example",),
    )
    session_id = await _escalation_row(
        admin_id,
        instance_id,
        lead_id,
        signal="explicit_human_request",
        gate="intake",
        reasons=["moderation:threats_violence", "threatened to come to the office"],
    )

    await _fire(
        admin_id,
        instance_id,
        lead_id,
        signal="explicit_human_request",
        gate="intake",
        session_id=session_id,
    )

    (page,) = email_fake.sent
    assert page["to"] == "sales@acme.example"
    assert str(page["subject"]).startswith("SAFETY: a threat of violence")
    body = str(page["body"])
    assert "Why: SAFETY — a threat of violence" in body
    assert "Details: threatened to come to the office" in body
    assert "moderation:" not in body
    assert "local authorities" in body
    assert "asked" not in body.lower() and "speak to a person" not in body.lower()


def _notification(**overrides: object) -> EscalationNotification:
    fields: dict[str, object] = {
        "admin_id": uuid.uuid4(),
        "instance_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "signal": "high_value_lead",
        "gate": "outcome",
        "business_name": "Acme Patios",
        "lead_name": "Dana Lee",
        "lead_contact": "+15550001111",
    }
    fields.update(overrides)
    return EscalationNotification(**fields)  # type: ignore[arg-type]


async def test_a_self_harm_token_renders_as_an_urgent_safety_text_and_a_safety_email():
    n = _notification(
        signal="explicit_human_request", gate="intake", reasons=("moderation:self_harm",)
    )
    sms = sms_notifier.SmsNotifier().format(n)
    assert sms.startswith("Acme Patios: URGENT SAFETY — a customer may be at risk of self-harm.")
    assert "explicit_human_request" not in sms and "asked" not in sms.lower()
    assert "emergency services" in sms
    assert "Why:" not in sms and "moderation:" not in sms  # the token is not a reason
    email = email_notifier.EmailNotifier()
    assert email.subject(n) == "SAFETY: a customer may be at risk of self-harm — Luciel needs you"
    body = email.format(n)
    assert "Why: SAFETY — a customer may be at risk of self-harm" in body
    assert "Details:" not in body and "moderation:" not in body
    assert "emergency services" in body
    assert "asked" not in body.lower() and "speak to a person" not in body.lower()


async def test_a_threats_token_renders_the_violence_label_on_both_channels():
    n = _notification(
        signal="explicit_human_request",
        gate="intake",
        reasons=("moderation:threats_violence", "threatened staff"),
    )
    sms = sms_notifier.SmsNotifier().format(n)
    assert sms.startswith("Acme Patios: URGENT SAFETY — a threat of violence.")
    assert "Why: threatened staff." in sms and "moderation:" not in sms
    assert "local authorities" in sms
    email = email_notifier.EmailNotifier()
    assert email.subject(n).startswith("SAFETY: a threat of violence")
    body = email.format(n)
    assert "Why: SAFETY — a threat of violence" in body
    assert "Details: threatened staff" in body and "moderation:" not in body
    assert "local authorities" in body


@pytest.mark.parametrize(
    "reason",
    ["abusive language toward the assistant or business", "asked for something illegal"],
)
async def test_a_flagged_row_says_what_was_flagged(reason: str):
    """The two D2 keep-answering categories, worded as the runtime writes them."""
    n = _notification(signal="content_flagged", gate="intake", reasons=(reason,))
    email = email_notifier.EmailNotifier()
    assert email.subject(n) == "Luciel needs you: a message was flagged"
    body = email.format(n)
    assert f"Why: a message was flagged — {reason}" in body
    assert "keeps answering" in body
    sms = sms_notifier.SmsNotifier().format(n)
    assert f"Acme Patios: Luciel escalated (content_flagged: {reason})." in sms


async def test_an_ask_bound_row_lists_the_missing_fields():
    """The §3.3.7 ask bound writes the field names still missing; the page carries
    them verbatim so the owner knows what to ask for."""
    reason = f"still missing email, phone after {ASK_ATTEMPT_BOUND} asks"
    n = _notification(signal="cannot_answer", gate="outcome", reasons=(reason,))
    body = email_notifier.EmailNotifier().format(n)
    assert "Why: Luciel could not answer confidently" in body
    assert f"Details: {reason}" in body
    assert f"Why: {reason}." in sms_notifier.SmsNotifier().format(n)
