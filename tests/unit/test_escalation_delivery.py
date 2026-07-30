"""Escalation notification DELIVERY (Architecture §3.5.3 / §3.5.4 / §3.5.5).

CJ Phase 6 promises the owner hears about a hot lead without logging in. These tests pin
that promise end-to-end from the fired signal: the right contact is reached on the right
channel, the cc-owner copy goes out, and a channel that cannot deliver says so loudly in
the audit log instead of dropping the escalation on the floor.
"""

from __future__ import annotations

import uuid

import pytest
from app.connections.secrets import secret_ref_for
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance
from app.models.lead import Lead
from app.notifications import email_notifier, sms_notifier
from app.notifications.escalation_dispatch import notify_escalation
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

OWNER_EMAIL = "owner@acme.example"


class FakeEmailSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, str | None]] = []

    async def send(self, *, to, subject, body_text, from_address=None):
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
) -> uuid.UUID:
    """One verified account + one Luciel with the given escalation pillar, plus a lead."""
    lead_id = uuid.uuid4()
    async with tenant_session(admin_id) as s:
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
                contact_identifier="dana@lead.example",
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
) -> list[AdminAuditLog]:
    async with tenant_session(admin_id) as s:
        await notify_escalation(
            s,
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=uuid.uuid4(),
            resolved_lead_id=lead_id,
            signal=signal,
            gate="outcome",
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
        admin_id, instance_id, escalation={"primaryEmail": "sales@acme.example"}
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
