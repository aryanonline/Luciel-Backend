"""Round 7 WP-3b — live takeover truth on every channel, the ROUTE half.

POST /api/v1/dashboard/conversations/{id}/messages (the owner's reply while holding
a conversation), take-over, and the conversation list. The channel half lives in
tests/unit/test_takeover_reply_channels.py, the ingress half in
tests/integration/test_takeover_reply_context.py.

  * item 1: the reply goes to an identifier of the SESSION's channel kind — after a
    widget-then-SMS unification the lead record is keyed on the older email, and the
    SMS reply went there. No identifier of that kind → ``no_recipient_for_channel``,
    never a send to another channel.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from app.connections.secrets import secret_ref_for
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.conversation import Session as SessionModel
from app.models.instance import Instance, LucielState
from app.models.lead import Lead, LeadIdentifier, LeadState
from sqlalchemy import select

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

LEAD_EMAIL = "dana@lead.example"
LEAD_PHONE = "+15557778888"
TENANT_NUMBER = "+15550001111"
TENANT_EMAIL = "hello@acmepatios.example"
PAGE_ID = "page-4242"
PSID = "psid-9001"
REPLY = "Ana here — Tuesday at 10 works."
ALL_CHANNELS = ("widget", "email", "sms", "voice", "whatsapp", "messenger", "instagram")


async def _signup_verify_login(client) -> uuid.UUID:
    from app.auth import access

    email = f"takeover-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return acct.id


@dataclass(frozen=True)
class Seeded:
    instance_id: uuid.UUID
    session_id: uuid.UUID
    lead_id: uuid.UUID


async def _seed(
    admin: uuid.UUID,
    *,
    channel: str,
    contact: str | None,
    identifiers: tuple[tuple[str, str], ...] = (),
    sms_status: str | None = None,
    email_provider: str | None = None,
    email_status: str = "connected",
    meta_status: str | None = None,
    **session_fields: object,
) -> Seeded:
    """One tenant with a Luciel, a lead, a session on ``channel`` resolved to that lead,
    and whichever sender connections the case needs.

    ``contact`` is the lead record's ``contact_identifier`` (the identity it was keyed
    on first); ``identifiers`` are the (kind, value) rows the lead is ALSO known by —
    what a unification leaves behind (confirmed by the transport that named them)."""
    iid, sid, lead_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    async with tenant_session(admin) as s:
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="Acme Patios",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[{"id": c, "enabled": True} for c in ALL_CHANNELS],
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
                name="Dana",
                contact_identifier=contact,
                state=LeadState.active,
            )
        )
        for kind, value in identifiers:
            s.add(
                LeadIdentifier(
                    id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    lead_id=lead_id,
                    kind=kind,
                    value=value,
                    confirmed_at=now,
                    source="transport",
                )
            )
        s.add(
            SessionModel(
                id=sid,
                admin_id=admin,
                instance_id=iid,
                channel=channel,
                mode="ai",
                resolved_lead_id=lead_id,
                summary="Asked about a quote",
                **session_fields,  # type: ignore[arg-type]
            )
        )
        if sms_status is not None:
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type="sms_sender",
                    auth_class="api_key",
                    provider="twilio",
                    status=sms_status,
                    secret_ref=secret_ref_for(admin, iid, "sms_sender"),
                    non_secret_config={"destination": TENANT_NUMBER},
                )
            )
        if email_provider is not None:
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type="email_sender",
                    auth_class="oauth_token" if email_provider == "outlook" else "api_key",
                    provider=email_provider,
                    status=email_status,
                    secret_ref=secret_ref_for(admin, iid, "email_sender"),
                    non_secret_config={"address": TENANT_EMAIL, "destination": TENANT_EMAIL},
                )
            )
        if meta_status is not None:
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type="channel_auth",
                    auth_class="oauth_token",
                    provider="meta",
                    status=meta_status,
                    secret_ref=secret_ref_for(admin, iid, "channel_auth"),
                    non_secret_config={"destinations": {"messenger": PAGE_ID}},
                )
            )
        await s.flush()
    return Seeded(iid, sid, lead_id)


class FakeTwilio:
    """Tenant Twilio stand-in: records the one send; ``accept=False`` models Twilio
    handing back no SID (the adapter reports ``twilio_rejected``)."""

    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.sent: dict[str, str] = {}

    async def send_sms(self, from_number: str, to: str, body: str) -> str | None:
        self.sent.update(from_number=from_number, to=to, body=body)
        return "SM-takeover-1" if self.accept else None


@pytest.fixture
def twilio(monkeypatch) -> FakeTwilio:
    from app.channels import tenant_twilio

    fake = FakeTwilio()

    async def _tenant_sender(*a, **kw):
        return fake

    monkeypatch.setattr(tenant_twilio, "build_tenant_sms_sender", _tenant_sender)
    return fake


class FakePlatformEmail:
    """Platform SES stand-in whose ``send`` accepts threading headers."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, *, to, subject, body_text, from_address=None, headers=None, tags=None):
        self.sent.append(
            {
                "to": to,
                "subject": subject,
                "body": body_text,
                "from_address": from_address,
                "headers": headers,
            }
        )
        return f"ses-{len(self.sent)}"


@pytest.fixture
def platform_email(monkeypatch) -> FakePlatformEmail:
    from app.notifications import email_sender as sender_mod

    fake = FakePlatformEmail()
    monkeypatch.setattr(sender_mod, "build_email_sender", lambda: fake)
    return fake


async def _take_over_and_reply(client, sid: uuid.UUID, text: str = REPLY) -> dict:
    rt = await client.post(f"/api/v1/dashboard/conversations/{sid}/take-over")
    assert rt.status_code == 200, rt.text
    r = await client.post(f"/api/v1/dashboard/conversations/{sid}/messages", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


async def _sent_audit_payloads(admin: uuid.UUID) -> list[dict]:
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog)
                    .where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "human_agent_message_sent",
                    )
                    .order_by(AdminAuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
    return [dict(r.event_payload) for r in rows]


# --- item 1: the destination is the SESSION's channel kind ---------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_sms_reply_to_a_widget_then_sms_unified_lead_goes_to_the_phone(client, twilio):
    """The live condition: Dana first wrote on the widget (record keyed on her email),
    later texted; the unification kept the older email-keyed row and re-pointed her
    phone into lead_identifiers. The owner's reply on the SMS conversation used to be
    sent to ``contact_identifier`` — the email — as if it were a phone number."""
    admin = await _signup_verify_login(client)
    seeded = await _seed(
        admin,
        channel="sms",
        contact=LEAD_EMAIL,
        identifiers=(("email", LEAD_EMAIL), ("phone", LEAD_PHONE)),
        sms_status="connected",
    )

    body = await _take_over_and_reply(client, seeded.session_id)

    assert body["delivered"] is True
    assert body["deliveryDetail"] is None
    assert twilio.sent == {"from_number": TENANT_NUMBER, "to": LEAD_PHONE, "body": REPLY}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_sms_reply_with_no_phone_on_record_is_refused_not_rerouted(client, twilio):
    """An email-only record on an SMS session (the identifier rows were lost, or the
    session predates them): nothing this channel can deliver to. The reply is kept
    in the transcript and the owner reads the honest wire word; no text is attempted
    to the email, and nothing goes to the email channel either."""
    admin = await _signup_verify_login(client)
    seeded = await _seed(
        admin,
        channel="sms",
        contact=LEAD_EMAIL,
        identifiers=(("email", LEAD_EMAIL),),
        sms_status="connected",
    )

    body = await _take_over_and_reply(client, seeded.session_id)

    assert body["delivered"] is False
    assert body["deliveryDetail"] == "no_recipient_for_channel"
    assert twilio.sent == {}
    msgs = await client.get(f"/api/v1/dashboard/conversations/{seeded.session_id}/messages")
    assert [m["role"] for m in msgs.json()] == ["human_agent"]
    (payload,) = await _sent_audit_payloads(admin)
    assert payload["delivered"] is False
    assert payload["delivery_detail"] == "no_recipient_for_channel"
    assert LEAD_EMAIL not in str(payload)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_email_reply_to_a_phone_keyed_lead_goes_to_the_email_identifier(
    client, platform_email
):
    """The mirror case: an SMS-first lead (record keyed on the phone) who later wrote
    an email. The reply on the EMAIL conversation goes to the email identifier, not
    to the phone number the record happens to be keyed on."""
    admin = await _signup_verify_login(client)
    seeded = await _seed(
        admin,
        channel="email",
        contact=LEAD_PHONE,
        identifiers=(("phone", LEAD_PHONE), ("email", LEAD_EMAIL)),
        email_provider="ses",
    )

    body = await _take_over_and_reply(client, seeded.session_id)

    assert body["delivered"] is True
    (sent,) = platform_email.sent
    assert sent["to"] == LEAD_EMAIL
    assert sent["from_address"] == TENANT_EMAIL


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_volunteered_phone_beside_an_email_key_reaches_the_sms_reply(client, twilio):
    """The record's own ``phone`` column (a typed number, F111) is an identifier of
    the SMS kind too — used before any identifier row, never the email key."""
    admin = await _signup_verify_login(client)
    seeded = await _seed(admin, channel="sms", contact=LEAD_EMAIL, sms_status="connected")
    async with tenant_session(admin) as s:
        lead = (await s.execute(select(Lead).where(Lead.id == seeded.lead_id))).scalar_one()
        lead.phone = LEAD_PHONE
        await s.flush()

    body = await _take_over_and_reply(client, seeded.session_id)

    assert body["delivered"] is True
    assert twilio.sent["to"] == LEAD_PHONE
