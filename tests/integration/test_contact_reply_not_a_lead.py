"""An escalation CONTACT texting back on the tenant's number is not a lead (audit F051)
— and only when it IS texting back (round 7 WP-4b, item 5).

The owner's escalation text says "reply to this number". When they did, their reply
came in on the same Twilio webhook as every customer text, opened a session, ran the
LLM, wrote a lead named after the owner's own phone number, and billed a conversation.
The inbound edge recognises the configured SMS contacts before admission: no session,
no runtime, no lead, no billing — just an ``escalation_acked`` audit row.

Then the rule over-reached: EVERY text from a configured number was dropped as a
"reply", so a manager texting the business from their own phone was never answered.
A text from a contact is a reply only when it correlates with a page to that number
inside 24 hours, or when it is the 6-digit confirmation code the number still owes
(item 1 — texting the code back proves the number without the dashboard). Anything
else is a customer message and runs the runtime.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.audit import log as audit
from app.channels import inbound_dispatch, rate_limit, routing
from app.channels.base import InboundMessage
from app.channels.number_operability import NullOperabilityProbe
from app.channels.phone_lifecycle import attach_sms_number
from app.channels.pipeline import ContactReply, ingest
from app.channels.signatures import twilio_signature
from app.channels.sms_adapter import SmsAdapter
from app.db.session import tenant_session
from app.models.audit import ActorType, AdminAuditLog
from app.models.conversation import Session as ConvSession
from app.models.email_incident import EscalationContactConfirmation
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.lead import Lead
from app.notifications import contact_confirmation
from sqlalchemy import func, select

from tests.conftest import superuser_exec

pytestmark = [pytest.mark.asyncio]

BYO_NUMBER = "+15551239999"
PRIMARY_CONTACT = "+15550000009"
AFTER_HOURS_CONTACT = "+15550000010"
CUSTOMER = "+15550007777"
CODE = "482913"


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
                team_availability={
                    "enabled": True,
                    "afterHours": {
                        "escalation": {"channel": "sms", "contactSms": AFTER_HOURS_CONTACT}
                    },
                },
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


async def _paged(admin: uuid.UUID, iid: uuid.UUID, number: str, *, ago: timedelta) -> None:
    """The trail the dispatcher leaves when it pages a number by SMS."""
    async with tenant_session(admin) as s:
        row = await audit.emit(
            s,
            admin_id=admin,
            instance_id=iid,
            event_type="escalation_notification_sent",
            actor_type=ActorType.platform,
            payload={
                "signal": "high_value_lead",
                "gate": "outcome",
                "delivery_method": "sms",
                "recipient": number,
                "recipient_role": "primary",
            },
        )
        event_id = row.event_id
    # The audit log is append-only by design (scripts/lock_audit_log.sql): a page that
    # went out ``ago`` is modelled the way test_audit_chain backdates a row — as the
    # superuser, with the guard trigger stepped around for the one statement.
    when = (datetime.now(UTC) - ago).isoformat()
    superuser_exec("ALTER TABLE admin_audit_log DISABLE TRIGGER trg_audit_append_only")
    try:
        superuser_exec(
            f"UPDATE admin_audit_log SET created_at = '{when}' WHERE event_id = '{event_id}'"
        )
    finally:
        superuser_exec("ALTER TABLE admin_audit_log ENABLE TRIGGER trg_audit_append_only")


async def _code_outstanding(admin: uuid.UUID, number: str) -> None:
    async with tenant_session(admin) as s:
        s.add(
            EscalationContactConfirmation(
                admin_id=admin,
                kind="sms",
                value_normalized=number,
                code_hash=contact_confirmation._hash_code(admin, number, CODE),
                code_expires_at=datetime.now(UTC) + timedelta(minutes=10),
                last_code_sent_at=datetime.now(UTC),
            )
        )
        await s.flush()


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


async def _ingest(admin: uuid.UUID, *, sender: str, body: str):
    async with tenant_session(admin) as s:
        return await ingest(SmsAdapter(FakeTwilio()), _request(admin, sender=sender, body=body), s)


async def _count(admin: uuid.UUID, model) -> int:
    async with tenant_session(admin) as s:
        return int(
            await s.scalar(select(func.count()).select_from(model).where(model.admin_id == admin))
            or 0
        )


async def _audit(admin: uuid.UUID, event_type: str) -> list[AdminAuditLog]:
    async with tenant_session(admin) as s:
        return list(
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin, AdminAuditLog.event_type == event_type
                    )
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_text_from_a_recently_paged_contact_is_a_contact_reply_not_an_inbound():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    rate_limit._store = rate_limit.TokenBucketStore()
    await _paged(admin, iid, PRIMARY_CONTACT, ago=timedelta(hours=2))

    outcome = await _ingest(admin, sender=PRIMARY_CONTACT, body="On it")
    assert isinstance(outcome, ContactReply)
    assert outcome.contact_role == "primary"
    assert outcome.channel == "sms"
    assert outcome.confirmation_code is None

    # A customer on the same number is still admitted as an inbound (the gate is on the
    # sender, never on the tenant's number).
    customer = await _ingest(admin, sender=CUSTOMER, body="Are you open?")
    assert isinstance(customer, InboundMessage)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_text_from_a_contact_nobody_paged_is_a_customer_message():
    """The over-reach: with no page to reply to, the manager texting from their own
    phone is a customer, and the runtime answers them. A page older than the 24h
    window does not count either."""
    admin = uuid.uuid4()
    iid = await _seed(admin)
    rate_limit._store = rate_limit.TokenBucketStore()

    outcome = await _ingest(admin, sender=PRIMARY_CONTACT, body="Do you do Saturday hours?")
    assert isinstance(outcome, InboundMessage)
    assert outcome.customer_identifier == PRIMARY_CONTACT

    await _paged(admin, iid, PRIMARY_CONTACT, ago=timedelta(hours=25))
    stale = await _ingest(admin, sender=PRIMARY_CONTACT, body="Still there?")
    assert isinstance(stale, InboundMessage)

    # A six-digit text with no code outstanding is not a code — a customer message.
    digits = await _ingest(admin, sender=PRIMARY_CONTACT, body="482913")
    assert isinstance(digits, InboundMessage)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_after_hours_contact_replying_to_its_page_is_a_contact_reply():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    rate_limit._store = rate_limit.TokenBucketStore()
    await _paged(admin, iid, AFTER_HOURS_CONTACT, ago=timedelta(minutes=5))

    outcome = await _ingest(admin, sender=AFTER_HOURS_CONTACT, body="Got it")
    assert isinstance(outcome, ContactReply)
    assert outcome.contact_role == "after_hours"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_dispatching_a_contact_reply_records_the_ack_and_opens_nothing():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    rate_limit._store = rate_limit.TokenBucketStore()
    await _paged(admin, iid, PRIMARY_CONTACT, ago=timedelta(minutes=30))

    reply = await inbound_dispatch.dispatch_inbound_reply(
        SmsAdapter(FakeTwilio()),
        _request(admin, sender=PRIMARY_CONTACT, body="Thanks, calling them now"),
    )
    assert reply is None  # nothing to say back to the owner on the customer channel

    assert await _count(admin, ConvSession) == 0
    assert await _count(admin, Lead) == 0
    rows = await _audit(admin, "escalation_acked")
    assert len(rows) == 1
    payload = rows[0].event_payload
    assert payload["channel"] == "sms"
    assert payload["contact_role"] == "primary"
    # The owner's words are theirs: only the length rides the audit trail (§3.4.11).
    assert "Thanks" not in str(payload)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_texting_the_confirmation_code_back_proves_the_number(monkeypatch):
    """Item 1's second door: the contact answers the code from their own phone. No
    session, no lead — the number is confirmed and the trail says how."""
    admin = uuid.uuid4()
    await _seed(admin)
    rate_limit._store = rate_limit.TokenBucketStore()
    await _code_outstanding(admin, PRIMARY_CONTACT)

    wrong = await _ingest(admin, sender=PRIMARY_CONTACT, body="Code: 000000")
    assert isinstance(wrong, ContactReply) and wrong.confirmation_code == "000000"
    reply = await inbound_dispatch.dispatch_inbound_reply(
        SmsAdapter(FakeTwilio()), _request(admin, sender=PRIMARY_CONTACT, body="Code: 000000")
    )
    assert reply is None
    rejected = await _audit(admin, "escalation_contact_code_rejected")
    assert len(rejected) == 1 and rejected[0].event_payload["outcome"] == "wrong_code"
    assert "000000" not in str(rejected[0].event_payload)

    reply = await inbound_dispatch.dispatch_inbound_reply(
        SmsAdapter(FakeTwilio()), _request(admin, sender=PRIMARY_CONTACT, body=f" {CODE} ")
    )
    assert reply is None
    verified = await _audit(admin, "escalation_contact_verified")
    assert len(verified) == 1
    assert verified[0].event_payload["entered"] == "texted_back"
    assert verified[0].event_payload["number_suffix"] == PRIMARY_CONTACT[-4:]
    assert PRIMARY_CONTACT not in str(verified[0].event_payload)
    async with tenant_session(admin) as s:
        row = await contact_confirmation.state_for(s, admin, "sms", PRIMARY_CONTACT)
        assert row is not None and row.confirmed_at is not None and row.code_hash is None
    assert await _count(admin, ConvSession) == 0
    assert await _count(admin, Lead) == 0

    # Confirmed and not paged: the next text from that phone is a customer again.
    later = await _ingest(admin, sender=PRIMARY_CONTACT, body="What are your hours?")
    assert isinstance(later, InboundMessage)
