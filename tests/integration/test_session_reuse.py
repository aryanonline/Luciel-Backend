"""Conversation = ONE billed session on every channel (§3.4.8 / Legal §0.1).

The owner-reported defect these tests pin: on SMS/email/WhatsApp/Messenger/Instagram,
EVERY inbound message used to open a brand-new ``sessions`` row — so a 10-message text
thread billed as 10 conversations. The §3.4.8 model is one session per
(instance, participant, channel), closed by the channel's inactivity timeout; the
budget counter fires once per session (§3.4.1b).

Each test drives the REAL runtime entry (``run_inbound_message``) the channel webhooks
call, so what is proven is the wire-facing behavior, not a helper in isolation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.channels.base import InboundMessage
from app.channels.runtime_entry import run_inbound_message
from app.db.session import tenant_session
from app.models.budget import CountedSession
from app.models.conversation import Session as ConvSession
from app.models.instance import Instance, LucielState
from sqlalchemy import func, select, update

pytestmark = [pytest.mark.asyncio]


async def _seed(admin_id: uuid.UUID, instance_id: uuid.UUID) -> None:
    async with tenant_session(admin_id) as s:
        s.add(
            Instance(
                id=instance_id,
                admin_id=admin_id,
                name="Acme Patios",
                state=LucielState.active,
                channels=[{"id": "widget", "enabled": True}, {"id": "sms", "enabled": True}],
            )
        )
        await s.flush()


def _inbound(
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    channel: str,
    identifier: str,
    body: str = "Are you open on Sunday?",
) -> InboundMessage:
    return InboundMessage(
        admin_id=admin_id,
        instance_id=instance_id,
        channel=channel,
        session_id=None,
        customer_identifier=identifier,
        body=body,
        channel_metadata={},
    )


async def _run(msg: InboundMessage) -> None:
    async with tenant_session(msg.admin_id) as s:
        await run_inbound_message(s, msg)


async def _session_count(admin_id: uuid.UUID) -> int:
    async with tenant_session(admin_id) as s:
        return (
            await s.scalar(
                select(func.count())
                .select_from(ConvSession)
                .where(ConvSession.admin_id == admin_id)
            )
        ) or 0


async def _counted_conversations(admin_id: uuid.UUID) -> int:
    async with tenant_session(admin_id) as s:
        return (
            await s.scalar(
                select(func.count())
                .select_from(CountedSession)
                .where(CountedSession.admin_id == admin_id)
            )
        ) or 0


async def _age_all_sessions(admin_id: uuid.UUID, age: timedelta) -> None:
    """Push every session's last_message_at into the past — the deterministic clock
    the §3.4.8 timeout reads — so a follow-up lands after the window."""
    async with tenant_session(admin_id) as s:
        await s.execute(
            update(ConvSession)
            .where(ConvSession.admin_id == admin_id)
            .values(last_message_at=datetime.now(UTC) - age)
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize(
    ("channel", "identifier"),
    [
        ("sms", "+14165550143"),
        ("whatsapp", "+14165550143"),
        ("messenger", "PSID_1"),
        ("instagram", "IGSID_1"),
        ("email", "dana@lead.example"),
    ],
)
async def test_a_thread_within_the_window_is_one_session_and_one_billed_conversation(
    channel, identifier
):
    """Three messages from the same person inside the inactivity window = ONE session
    row and ONE counted conversation. This is the owner-reported overbilling defect."""
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    await _seed(admin_id, instance_id)

    for body in ("hi, are you open?", "and do you deliver?", "great — book me in"):
        await _run(_inbound(admin_id, instance_id, channel, identifier, body))

    assert await _session_count(admin_id) == 1
    assert await _counted_conversations(admin_id) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize(
    ("channel", "identifier", "age"),
    [
        # Asynchronous-messaging class: 4-hour window (§3.4.8).
        ("sms", "+14165550143", timedelta(hours=5)),
        ("messenger", "PSID_1", timedelta(hours=5)),
        # Asynchronous long-form: 24 hours.
        ("email", "dana@lead.example", timedelta(hours=25)),
    ],
)
async def test_a_message_after_the_inactivity_window_opens_a_new_session(channel, identifier, age):
    """A reopened thread after the channel timeout is a NEW session — a new budget
    unit (§3.4.8 'Reopened threads')."""
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    await _seed(admin_id, instance_id)

    await _run(_inbound(admin_id, instance_id, channel, identifier, "first contact"))
    await _age_all_sessions(admin_id, age)
    await _run(_inbound(admin_id, instance_id, channel, identifier, "hello again"))

    assert await _session_count(admin_id) == 2
    assert await _counted_conversations(admin_id) == 2


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_message_inside_the_window_does_not_split_even_hours_later():
    """3 hours is inside SMS's 4-hour window: still the same session."""
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    await _seed(admin_id, instance_id)

    await _run(_inbound(admin_id, instance_id, "sms", "+14165550143", "first"))
    await _age_all_sessions(admin_id, timedelta(hours=3))
    await _run(_inbound(admin_id, instance_id, "sms", "+14165550143", "still me"))

    assert await _session_count(admin_id) == 1
    assert await _counted_conversations(admin_id) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_different_participants_never_share_a_session():
    """Two numbers texting in the same minute are two conversations — the key is the
    participant, so cross-lead bleed (§3.4.9) is structurally impossible here."""
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    await _seed(admin_id, instance_id)

    await _run(_inbound(admin_id, instance_id, "sms", "+14165550143"))
    await _run(_inbound(admin_id, instance_id, "sms", "+16045550177"))

    assert await _session_count(admin_id) == 2
    assert await _counted_conversations(admin_id) == 2


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_same_identifier_on_two_channels_holds_two_sessions():
    """§3.4.8: the session key includes the channel. The same person texting AND
    WhatsApp-ing holds one session per transport."""
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    await _seed(admin_id, instance_id)

    await _run(_inbound(admin_id, instance_id, "sms", "+14165550143"))
    await _run(_inbound(admin_id, instance_id, "whatsapp", "+14165550143"))

    assert await _session_count(admin_id) == 2


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset", "two_tenants")
async def test_the_same_number_on_two_tenants_is_two_isolated_sessions(two_tenants):
    """Tenant isolation of the participant key: one person texting two businesses is
    an independent lead at each (Vision §5) — and neither tenant's session count or
    billing is affected by the other's."""
    admin_a, admin_b = two_tenants
    ia, ib = uuid.uuid4(), uuid.uuid4()
    await _seed(admin_a, ia)
    await _seed(admin_b, ib)

    await _run(_inbound(admin_a, ia, "sms", "+14165550143"))
    await _run(_inbound(admin_b, ib, "sms", "+14165550143"))
    await _run(_inbound(admin_a, ia, "sms", "+14165550143", "second msg to A"))

    assert await _session_count(admin_a) == 1
    assert await _counted_conversations(admin_a) == 1
    assert await _session_count(admin_b) == 1
    assert await _counted_conversations(admin_b) == 1

    # The two tenants' sessions carry DIFFERENT participant keys for the same number.
    async with tenant_session(admin_a) as s:
        key_a = (
            await s.execute(
                select(ConvSession.participant_key).where(ConvSession.admin_id == admin_a)
            )
        ).scalar_one()
    async with tenant_session(admin_b) as s:
        key_b = (
            await s.execute(
                select(ConvSession.participant_key).where(ConvSession.admin_id == admin_b)
            )
        ).scalar_one()
    assert key_a and key_b and key_a != key_b


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_explicitly_ended_session_is_never_resumed():
    """ended_at set (finalization sweep or close beacon) means ended — a follow-up
    inside the inactivity window still opens a NEW session (§3.4.8 end signal)."""
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    await _seed(admin_id, instance_id)

    await _run(_inbound(admin_id, instance_id, "sms", "+14165550143", "first"))
    async with tenant_session(admin_id) as s:
        await s.execute(
            update(ConvSession)
            .where(ConvSession.admin_id == admin_id)
            .values(ended_at=datetime.now(UTC))
        )
    await _run(_inbound(admin_id, instance_id, "sms", "+14165550143", "back again"))

    assert await _session_count(admin_id) == 2
