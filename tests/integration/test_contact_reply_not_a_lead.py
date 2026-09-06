"""An escalation CONTACT texting back on the tenant's number is not a lead (audit F051).

The owner's escalation text says "reply to this number". When they did, their reply
came in on the same Twilio webhook as every customer text, opened a session, ran the
LLM, wrote a lead named after the owner's own phone number, and billed a conversation.
The inbound edge now recognises the configured SMS contacts before admission: no
session, no runtime, no lead, no billing — just an ``escalation_acked`` audit row so the
dashboard's escalation trail shows the acknowledgement.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels import inbound_dispatch, rate_limit, routing
from app.channels.base import InboundMessage
from app.channels.number_operability import NullOperabilityProbe
from app.channels.phone_lifecycle import attach_sms_number
from app.channels.pipeline import ContactReply, ingest
from app.channels.signatures import twilio_signature
from app.channels.sms_adapter import SmsAdapter
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.conversation import Session as ConvSession
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.lead import Lead
from sqlalchemy import func, select

pytestmark = [pytest.mark.asyncio]

BYO_NUMBER = "+15551239999"
PRIMARY_CONTACT = "+15550000009"
CUSTOMER = "+15550007777"


class FakeTwilio:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send_sms(self, from_number, to, body):
        self.sent.append((from_number, to, body))
        return "SM123"


async def _seed(admin: uuid.UUID) -> uuid.UUID:
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@x.com",
                email_lower=f"{admin}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        iid = uuid.uuid4()
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="L",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[{"id": "sms", "enabled": True}],
                tools=[],
                # Saved the way an owner types it — the match is on digits, not formatting.
                escalation={"primarySms": "+1 (555) 000-0009", "preferredChannel": "sms"},
                personality={},
            )
        )
        await s.flush()
        conn = await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=BYO_NUMBER,
            probe=NullOperabilityProbe(operable_numbers={BYO_NUMBER}),
        )
        assert conn.non_secret_config["destination"] == BYO_NUMBER
        return iid


def _request(admin: uuid.UUID, *, sender: str, body: str) -> dict:
    token = "tw"  # noqa: S105
    url = "https://x/sms"

    async def resolve(ct, dest):
        async with tenant_session(admin) as s:
            return await routing.resolve_destination(s, ct, dest)

    params = {"To": BYO_NUMBER, "From": sender, "Body": body, "MessageSid": "S1"}
    return {
        "auth_token": token,
        "url": url,
        "params": params,
        "x_twilio_signature": twilio_signature(token, url, params),
        "resolve": resolve,
        "source_ip": "203.0.113.9",
    }


async def _count(admin: uuid.UUID, model) -> int:
    async with tenant_session(admin) as s:
        return int(
            await s.scalar(select(func.count()).select_from(model).where(model.admin_id == admin))
            or 0
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_text_from_the_primary_contact_is_a_contact_reply_not_an_inbound():
    admin = uuid.uuid4()
    await _seed(admin)
    rate_limit._store = rate_limit.TokenBucketStore()

    async with tenant_session(admin) as s:
        outcome = await ingest(
            SmsAdapter(FakeTwilio()), _request(admin, sender=PRIMARY_CONTACT, body="On it"), s
        )
    assert isinstance(outcome, ContactReply)
    assert outcome.contact_role == "primary"
    assert outcome.channel == "sms"

    # A customer on the same number is still admitted as an inbound (the gate is on the
    # sender, never on the tenant's number).
    async with tenant_session(admin) as s:
        customer = await ingest(
            SmsAdapter(FakeTwilio()), _request(admin, sender=CUSTOMER, body="Are you open?"), s
        )
    assert isinstance(customer, InboundMessage)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_dispatching_a_contact_reply_records_the_ack_and_opens_nothing():
    admin = uuid.uuid4()
    await _seed(admin)
    rate_limit._store = rate_limit.TokenBucketStore()

    reply = await inbound_dispatch.dispatch_inbound_reply(
        SmsAdapter(FakeTwilio()),
        _request(admin, sender=PRIMARY_CONTACT, body="Thanks, calling them now"),
    )
    assert reply is None  # nothing to say back to the owner on the customer channel

    assert await _count(admin, ConvSession) == 0
    assert await _count(admin, Lead) == 0
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "escalation_acked",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    payload = rows[0].event_payload
    assert payload["channel"] == "sms"
    assert payload["contact_role"] == "primary"
    # The owner's words are theirs: only the length rides the audit trail (§3.4.11).
    assert "Thanks" not in str(payload)
