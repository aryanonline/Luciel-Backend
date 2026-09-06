"""A channel the owner switched OFF admits no new conversation (2026-09-05 audit F058).

Only the voice route ever checked the pillar's ``enabled`` flag; SMS, email, WhatsApp,
Messenger and Instagram kept answering — and billing — while the configure page said
"off". The gate lives in the shared runtime entry so every text channel agrees with the
pillar, and it honours §3.8.7 rule E.1: a conversation already in flight when the
channel is switched off finishes on its channel instead of cutting the lead off.

The same entry also stops fabricating a bot line during a human takeover (F057): a
human-controlled session answers with an EMPTY reply, and only the at-cap turn gets
the canned at-cap line.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels.base import InboundMessage
from app.channels.runtime_entry import run_inbound_message
from app.db.session import tenant_session
from app.models.conversation import Session as ConvSession
from app.models.instance import (
    ALWAYS_ENABLED_CHANNELS,
    Instance,
    LucielState,
    channel_enabled,
)
from app.runtime import handoff
from app.runtime.orchestrator import AT_CAP_REPLY
from sqlalchemy import func, select, update

pytestmark = [pytest.mark.asyncio]

TEXT_CHANNELS = ["sms", "email", "whatsapp", "messenger", "instagram"]


def _instance(admin: uuid.UUID, iid: uuid.UUID, channels: list[dict[str, object]]) -> Instance:
    return Instance(
        id=iid,
        admin_id=admin,
        name="Acme Patios",
        state=LucielState.active,
        channels=channels,
        tools=[],
        escalation={},
        personality={},
    )


def _inbound(
    admin: uuid.UUID, iid: uuid.UUID, channel: str, *, who: str | None = None
) -> InboundMessage:
    default = "+15557778888" if channel == "sms" else "dana@lead.example"
    return InboundMessage(
        admin_id=admin,
        instance_id=iid,
        channel=channel,
        session_id=None,
        customer_identifier=who or default,
        body="Are you open on Sunday?",
        channel_metadata={},
    )


async def _session_count(admin: uuid.UUID) -> int:
    async with tenant_session(admin) as s:
        return int(
            await s.scalar(
                select(func.count()).select_from(ConvSession).where(ConvSession.admin_id == admin)
            )
            or 0
        )


# --- the predicate -------------------------------------------------------------- #
@pytest.mark.parametrize("channel", TEXT_CHANNELS + ["voice"])
def test_a_channel_is_off_unless_the_pillar_says_on(channel: str):
    inst = _instance(uuid.uuid4(), uuid.uuid4(), [{"id": channel, "enabled": False}])
    assert channel_enabled(inst, channel) is False
    inst.channels = []
    assert channel_enabled(inst, channel) is False
    inst.channels = [{"id": channel, "enabled": True}]
    assert channel_enabled(inst, channel) is True


def test_the_widget_is_the_front_door_and_has_no_off_switch():
    assert "widget" in ALWAYS_ENABLED_CHANNELS
    inst = _instance(uuid.uuid4(), uuid.uuid4(), [])
    assert channel_enabled(inst, "widget") is True


def test_the_legacy_combined_channel_reads_as_on_when_either_half_is_on():
    inst = _instance(
        uuid.uuid4(),
        uuid.uuid4(),
        [{"id": "instagram", "enabled": True}, {"id": "messenger", "enabled": False}],
    )
    assert channel_enabled(inst, "instagram_messenger") is True
    inst.channels = [{"id": "instagram", "enabled": False}, {"id": "messenger", "enabled": False}]
    assert channel_enabled(inst, "instagram_messenger") is False


# --- the runtime gate ------------------------------------------------------------ #
@pytest.mark.parametrize("channel", TEXT_CHANNELS)
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_switched_off_channel_opens_no_session_and_answers_nothing(channel: str):
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(_instance(admin, iid, [{"id": channel, "enabled": False}]))
        await s.flush()

    async with tenant_session(admin) as s:
        reply = await run_inbound_message(s, _inbound(admin, iid, channel))

    assert reply.render_state == "paused"
    assert reply.reply_text == ""
    assert reply.session_id == ""
    assert await _session_count(admin) == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_conversation_in_flight_finishes_after_the_channel_is_switched_off():
    """§3.8.7 rule E.1: the lead mid-sentence keeps their conversation; only NEW
    conversations are held. Same drain rule the pause gate applies."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(_instance(admin, iid, [{"id": "sms", "enabled": True}]))
        await s.flush()

    async with tenant_session(admin) as s:
        first = await run_inbound_message(s, _inbound(admin, iid, "sms"))
    assert first.render_state == "active"
    assert first.session_id
    assert first.is_new_session is True

    async with tenant_session(admin) as s:
        await s.execute(
            update(Instance)
            .where(Instance.id == iid)
            .values(channels=[{"id": "sms", "enabled": False}])
        )

    async with tenant_session(admin) as s:
        second = await run_inbound_message(s, _inbound(admin, iid, "sms"))
    assert second.render_state == "active"
    assert second.session_id == first.session_id  # the same conversation, drained
    assert second.is_new_session is False

    async with tenant_session(admin) as s:
        stranger = await run_inbound_message(s, _inbound(admin, iid, "sms", who="+15550009999"))
    assert stranger.render_state == "paused"
    assert stranger.session_id == ""
    assert await _session_count(admin) == 1


# --- no fabricated line during a takeover ------------------------------------------ #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_human_controlled_session_answers_with_an_empty_reply_not_a_bot_line():
    """§3.4.12: once a person holds the conversation, the person answers. The runtime
    used to coalesce the loop's empty reply into the at-cap line, so every channel sent
    "we're at capacity"-style text on every takeover turn (audit F057/F145)."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(_instance(admin, iid, [{"id": "sms", "enabled": True}]))
        await s.flush()

    async with tenant_session(admin) as s:
        first = await run_inbound_message(s, _inbound(admin, iid, "sms"))
    assert first.reply_text  # Luciel answered the opening turn
    assert first.handed_off is False

    async with tenant_session(admin) as s:
        await handoff.take_over(
            s,
            admin_id=admin,
            instance_id=iid,
            session_id=uuid.UUID(first.session_id),
            actor_user_id=admin,
        )

    async with tenant_session(admin) as s:
        during = await run_inbound_message(s, _inbound(admin, iid, "sms"))
    assert during.session_id == first.session_id
    assert during.handed_off is True
    assert during.render_state == "active"
    assert during.reply_text == ""
    assert AT_CAP_REPLY not in during.reply_text
