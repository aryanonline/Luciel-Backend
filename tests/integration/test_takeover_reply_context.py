"""Ingress persists what a LATER human reply needs to go out AS the conversation
(round 7 WP-3b items 2 and 7; migration 0066; ``inbound_dispatch.persist_reply_context``).

The owner's takeover reply arrives hours after the inbound is gone. Without this it
re-derived everything at reply time: email left as a fresh "Re: your inquiry" thread
(no subject, no message id to reply under), and a Meta reply resolved its surface from
the channel name instead of the surface the customer actually wrote to.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels import inbound_dispatch
from app.channels.base import InboundMessage
from app.db.session import tenant_session
from app.models.conversation import Session as ConvSession
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select

PAGE_ID = "page-4242"
PSID = "psid-9001"


async def _seed(admin: uuid.UUID, *channels: str) -> uuid.UUID:
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
                channels=[{"id": c, "enabled": True} for c in channels],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
        return iid


def _inbound(
    admin: uuid.UUID, iid: uuid.UUID, *, channel: str, identifier: str, text: str, **metadata
) -> InboundMessage:
    return InboundMessage(
        admin_id=admin,
        instance_id=iid,
        channel=channel,
        session_id=None,
        customer_identifier=identifier,
        body=text,
        channel_metadata=metadata,
    )


async def _session(admin: uuid.UUID, session_id: str) -> ConvSession:
    async with tenant_session(admin) as s:
        return (
            await s.execute(
                select(ConvSession).where(
                    ConvSession.admin_id == admin, ConvSession.id == uuid.UUID(session_id)
                )
            )
        ).scalar_one()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_inbound_email_persists_its_subject_and_provider_message_id():
    """BYO mailbox: the Graph id is the ``/messages/{id}/reply`` handle, so it wins
    over the RFC Message-ID when both are known. A second mail in the same session
    moves both to the LATEST inbound — the reply hangs under the message it answers."""
    admin = uuid.uuid4()
    iid = await _seed(admin, "email")

    first = await inbound_dispatch.run_and_reply_full(
        _inbound(
            admin,
            iid,
            channel="email",
            identifier="dana@lead.example",
            text="Do you deliver on Sundays?",
            subject="Delivery?",
            graph_message_id="AAMkAGE1",
            internet_message_id="<first@lead.example>",
        )
    )
    assert first.session_id
    sess = await _session(admin, first.session_id)
    assert sess.inbound_subject == "Delivery?"
    assert sess.inbound_message_id == "AAMkAGE1"

    second = await inbound_dispatch.run_and_reply_full(
        _inbound(
            admin,
            iid,
            channel="email",
            identifier="dana@lead.example",
            text="And Saturdays?",
            subject="Re: Delivery? (and Saturdays)",
            graph_message_id="AAMkAGE2",
            internet_message_id="<second@lead.example>",
        )
    )
    assert second.session_id == first.session_id
    sess = await _session(admin, first.session_id)
    assert sess.inbound_subject == "Re: Delivery? (and Saturdays)"
    assert sess.inbound_message_id == "AAMkAGE2"
    # Email carries no Meta context; the columns stay honestly NULL.
    assert sess.meta_destination_id is None
    assert sess.meta_sender_id is None
    # The From address itself (migration 0074): a takeover reply answers THIS sender,
    # never another address the lead record also knows.
    assert sess.transport_sender == "dana@lead.example"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_legacy_ses_inbound_persists_the_rfc_message_id():
    """No Graph id on the SES path: the RFC 5322 Message-ID is what In-Reply-To needs."""
    admin = uuid.uuid4()
    iid = await _seed(admin, "email")

    reply = await inbound_dispatch.run_and_reply_full(
        _inbound(
            admin,
            iid,
            channel="email",
            identifier="dana@lead.example",
            text="Quote please",
            subject="Quote",
            internet_message_id="<ses-1@lead.example>",
        )
    )
    sess = await _session(admin, reply.session_id)
    assert sess.inbound_subject == "Quote"
    assert sess.inbound_message_id == "<ses-1@lead.example>"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize(
    ("channel", "metadata_key"),
    [("messenger", "destination_id"), ("instagram", "destination_id")],
)
async def test_an_inbound_meta_message_persists_the_surface_and_the_sender(channel, metadata_key):
    """The tenant surface the customer wrote TO (page / IG account) and the customer's
    platform-scoped id are what a later reply sends AS and TO."""
    admin = uuid.uuid4()
    iid = await _seed(admin, channel)

    reply = await inbound_dispatch.run_and_reply_full(
        _inbound(
            admin,
            iid,
            channel=channel,
            identifier=PSID,
            text="Hi, are you open today?",
            **{metadata_key: PAGE_ID, "mid": "m_1"},
        )
    )
    sess = await _session(admin, reply.session_id)
    assert sess.meta_destination_id == PAGE_ID
    assert sess.meta_sender_id == PSID
    assert sess.inbound_subject is None
    assert sess.inbound_message_id is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_inbound_whatsapp_message_persists_the_phone_number_id_as_the_surface():
    admin = uuid.uuid4()
    iid = await _seed(admin, "whatsapp")

    reply = await inbound_dispatch.run_and_reply_full(
        _inbound(
            admin,
            iid,
            channel="whatsapp",
            identifier="15557778888",
            text="Hi",
            phone_number_id="pn-31337",
        )
    )
    sess = await _session(admin, reply.session_id)
    assert sess.meta_destination_id == "pn-31337"
    assert sess.meta_sender_id == "15557778888"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_inbound_text_persists_its_sender_and_nothing_else():
    """SMS answers to the number the lead wrote from. That number is persisted raw
    (migration 0074 — the participant key is only a hash): a lead known by two phones
    gets the owner's takeover reply at the one in THIS conversation. Nothing else is
    stashed, nothing fabricated."""
    admin = uuid.uuid4()
    iid = await _seed(admin, "sms")

    reply = await inbound_dispatch.run_and_reply_full(
        _inbound(admin, iid, channel="sms", identifier="+15557778888", text="Hi")
    )
    sess = await _session(admin, reply.session_id)
    assert sess.transport_sender == "+15557778888"
    assert (
        sess.inbound_subject,
        sess.inbound_message_id,
        sess.meta_destination_id,
        sess.meta_sender_id,
    ) == (None, None, None, None)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_meta_message_never_fills_the_transport_sender():
    """The Meta sender lives in ``meta_sender_id`` (0066); the sms/voice/email column
    stays NULL so no reader mistakes a platform-scoped id for a phone or address."""
    admin = uuid.uuid4()
    iid = await _seed(admin, "messenger")

    reply = await inbound_dispatch.run_and_reply_full(
        _inbound(
            admin, iid, channel="messenger", identifier=PSID, text="Hi", destination_id=PAGE_ID
        )
    )
    sess = await _session(admin, reply.session_id)
    assert sess.meta_sender_id == PSID
    assert sess.transport_sender is None
