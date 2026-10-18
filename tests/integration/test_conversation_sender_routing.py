"""The person IN the conversation is who a takeover reply and the owner page name
(phase-2 security review, finding: takeover delivery routing).

WP-3c item 1 lets a first text that names a known email continue that lead — so one
lead is routinely known by two phones. The takeover reply (and the voice bridge, and
the escalation page's contact line) used to read the LEAD record and pick whichever
phone it was keyed on: the owner's message reached a number that was not in the
conversation, stamped delivered. These tests drive the real ingress (the raw sender
persisted on the session, migration 0074) and the real routes.
"""

from __future__ import annotations

import uuid

import pytest
from app.api.deps import Subject
from app.api.v1 import dashboard
from app.channels import inbound_dispatch
from app.channels.base import InboundMessage
from app.connections.secrets import secret_ref_for
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from app.models.conversation import Session as SessionModel
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.lead import Lead, LeadIdentifier, LeadState
from app.notifications.email_notifier import EmailNotifier
from app.notifications.escalation_dispatch import _build_notification
from app.runtime.sessions import participant_key_for
from app.schemas_api.conversations import HumanReplyRequest
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

PHONE_A = "+14165550111"  # the number Jane's record is keyed on
PHONE_B = "+14165550222"  # the number this conversation is on
TENANT_NUMBER = "+15550001111"


class _FakeTwilio:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send_sms(self, from_number: str, to: str, body: str) -> str | None:
        self.sent.append({"from_number": from_number, "to": to, "body": body})
        return f"SM-{len(self.sent)}"


@pytest.fixture
def twilio(monkeypatch) -> _FakeTwilio:
    from app.channels import tenant_twilio

    fake = _FakeTwilio()

    async def _tenant_sender(*a, **kw):
        return fake

    monkeypatch.setattr(tenant_twilio, "build_tenant_sms_sender", _tenant_sender)
    return fake


async def _seed_tenant(admin: uuid.UUID) -> uuid.UUID:
    iid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin.hex[:8]}@x.com",
                email_lower=f"{admin.hex[:8]}@x.com",
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
                embed_key_public_id=f"vm_live_{admin.hex[:10]}",
                channels=[{"id": "sms", "enabled": True}, {"id": "voice", "enabled": True}],
                tools=[],
                escalation={},
                personality={},
            )
        )
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="sms_sender",
                auth_class="api_key",
                provider="twilio",
                status="connected",
                secret_ref=secret_ref_for(admin, iid, "sms_sender"),
                non_secret_config={"destination": TENANT_NUMBER},
            )
        )
        await s.flush()
    return iid


async def _text(admin: uuid.UUID, iid: uuid.UUID, sender: str, body: str) -> uuid.UUID:
    reply = await inbound_dispatch.run_and_reply_full(
        InboundMessage(
            admin_id=admin,
            instance_id=iid,
            channel="sms",
            session_id=None,
            customer_identifier=sender,
            body=body,
            channel_metadata={},
        )
    )
    assert reply.session_id
    return uuid.UUID(reply.session_id)


async def _session(admin: uuid.UUID, sid: uuid.UUID) -> SessionModel:
    async with tenant_session(admin) as s:
        return (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()


def _subject(admin: uuid.UUID) -> Subject:
    return Subject(admin_id=admin, user_id=admin, session_epoch=0)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_takeover_reply_on_a_second_number_reaches_that_number(twilio):
    """Jane texted from A and gave her email; a text from B says it's Jane with that
    email and joins her record (owner-approved WP-3c item 1). The owner takes over
    the B conversation and replies: the text goes to B — the number in the
    conversation — not to A, the number the record is keyed on."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    first = await _text(admin, iid, PHONE_A, "Hi, I'm Jane, my email is jane@acme.com")
    second = await _text(admin, iid, PHONE_B, "it's Jane - jane@acme.com, any news?")

    jane = (await _session(admin, first)).resolved_lead_id
    sess = await _session(admin, second)
    assert jane is not None and sess.resolved_lead_id == jane  # one record, two phones
    assert sess.transport_sender == PHONE_B
    async with tenant_session(admin) as s:
        lead = (await s.execute(select(Lead).where(Lead.id == jane))).scalar_one()
        assert lead.contact_identifier == PHONE_A

    await dashboard.take_over(second, subject=_subject(admin))
    result = await dashboard.send_human_message(
        second, HumanReplyRequest(text="Ana here, Tuesday works."), subject=_subject(admin)
    )

    assert result.delivered is True
    assert [m["to"] for m in twilio.sent] == [PHONE_B]


async def _seed_two_phone_lead(
    admin: uuid.UUID, iid: uuid.UUID, *, channel: str, **session_fields: object
) -> tuple[uuid.UUID, uuid.UUID]:
    lead_id, sid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Lead(
                id=lead_id,
                admin_id=admin,
                instance_id=iid,
                name="Jane",
                contact_identifier=PHONE_A,
                state=LeadState.active,
            )
        )
        await s.flush()
        for number in (PHONE_A, PHONE_B):
            s.add(
                LeadIdentifier(
                    id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    lead_id=lead_id,
                    kind="phone",
                    value=number,
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
                **session_fields,  # type: ignore[arg-type]
            )
        )
        await s.flush()
    return lead_id, sid


async def _page_contact(admin: uuid.UUID, iid: uuid.UUID, lead_id: uuid.UUID, sid: uuid.UUID):
    async with tenant_session(admin) as s:
        notification, _inst, _owner = await _build_notification(
            s,
            admin_id=admin,
            instance_id=iid,
            session_id=sid,
            resolved_lead_id=lead_id,
            signal="explicit_human_request",
            gate="intake",
            score=None,
        )
    return notification, EmailNotifier().format(notification)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_escalation_page_names_the_number_in_the_conversation():
    """The owner page's "Who to reach" used to print the record's key: the owner then
    called A about a conversation B was having."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    lead_id, sid = await _seed_two_phone_lead(admin, iid, channel="sms", transport_sender=PHONE_B)

    notification, body = await _page_contact(admin, iid, lead_id, sid)

    assert notification.lead_contact == PHONE_B
    assert notification.customer_identifier == PHONE_B
    assert f"Contact: {PHONE_B}" in body
    assert PHONE_A not in body


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_pre_0074_session_pages_the_number_its_key_names():
    """No stored sender: the participant key names the number in the conversation."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    lead_id, sid = await _seed_two_phone_lead(
        admin, iid, channel="sms", participant_key=participant_key_for(admin, "sms", PHONE_B)
    )

    notification, _ = await _page_contact(admin, iid, lead_id, sid)

    assert notification.lead_contact == PHONE_B


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_session_whose_sender_cannot_be_told_pages_no_contact_not_the_record_key():
    """A key no identifier of the lead hashes to names nobody: "not captured" (the
    record's own volunteered facts still print on their own lines) — never A."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    lead_id, sid = await _seed_two_phone_lead(
        admin,
        iid,
        channel="sms",
        participant_key=participant_key_for(admin, "sms", "+14165550999"),
    )

    notification, body = await _page_contact(admin, iid, lead_id, sid)

    assert notification.lead_contact is None
    assert "Contact: not captured" in body
    assert PHONE_A not in body


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_widget_page_still_names_the_record_contact():
    """The widget carries no transport sender: the page reads the record as before."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    lead_id, sid = await _seed_two_phone_lead(admin, iid, channel="widget")

    notification, _ = await _page_contact(admin, iid, lead_id, sid)

    assert notification.lead_contact == PHONE_A
