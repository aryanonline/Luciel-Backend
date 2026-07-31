"""Pausing Luciel must pause EVERY channel, not just the web widget (BE P1-3, §3.6.2).

The non-widget channels (SMS, email, WhatsApp, Instagram/Messenger) share one runtime
entry, and its pause gate used to read ``inst.status``/``inst.paused_at`` — two fields
that do not exist on ``Instance``. Both ``getattr`` calls were permanently ``None``, so
the branch never fired: an owner who paused Luciel found it still answering leads,
burning their conversation budget and speaking on their behalf after being told to stop.

These tests pin the gate against the model's REAL state field, and pin that all three
runtime paths (channels, widget, voice) agree on what "paused" means — the drift between
them is what hid the bug.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels.base import InboundMessage
from app.channels.runtime_entry import run_inbound_message
from app.db.session import tenant_session
from app.models.conversation import Session as ConvSession
from app.models.instance import Instance, LucielState, is_paused
from sqlalchemy import func, select

pytestmark = [pytest.mark.asyncio]

NON_SERVING = (
    LucielState.paused,
    LucielState.luciel_grace_window,
    LucielState.luciel_hard_deleted,
)


async def _seed(admin_id: uuid.UUID, instance_id: uuid.UUID, state: LucielState) -> None:
    async with tenant_session(admin_id) as s:
        s.add(Instance(id=instance_id, admin_id=admin_id, name="Acme Patios", state=state))
        await s.flush()


def _inbound(admin_id: uuid.UUID, instance_id: uuid.UUID, channel: str) -> InboundMessage:
    return InboundMessage(
        admin_id=admin_id,
        instance_id=instance_id,
        channel=channel,
        session_id=None,
        customer_identifier="+15557778888" if channel == "sms" else "dana@lead.example",
        body="Are you open on Sunday?",
        channel_metadata={},
    )


@pytest.mark.parametrize("state", NON_SERVING)
@pytest.mark.parametrize("channel", ["sms", "email", "whatsapp", "instagram_messenger"])
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_paused_luciel_answers_nothing_on_any_channel(state, channel):
    """The owner-visible promise: paused means silent, on every channel."""
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    await _seed(admin_id, instance_id, state)

    async with tenant_session(admin_id) as s:
        reply = await run_inbound_message(s, _inbound(admin_id, instance_id, channel))

    assert reply.render_state == "paused"
    assert reply.reply_text == ""
    assert reply.session_id == ""


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_paused_luciel_opens_no_session_and_spends_no_budget():
    """No session row means no turn ran: no LLM call, and nothing counted against the
    tenant's conversation budget. A paused Luciel that still bills is worse than one
    that still talks."""
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    await _seed(admin_id, instance_id, LucielState.paused)

    async with tenant_session(admin_id) as s:
        await run_inbound_message(s, _inbound(admin_id, instance_id, "sms"))

    async with tenant_session(admin_id) as s:
        sessions = await s.scalar(
            select(func.count()).select_from(ConvSession).where(ConvSession.admin_id == admin_id)
        )
    assert sessions == 0


async def test_the_gate_reads_a_field_the_model_actually_has():
    """The original defect in one assertion: ``status``/``paused_at`` do not exist, so a
    gate written against them can never fire. Anything the gate reads must be a real
    mapped column."""
    assert "state" in Instance.__mapper__.columns
    assert "status" not in Instance.__mapper__.columns
    assert "paused_at" not in Instance.__mapper__.columns


@pytest.mark.parametrize("state", list(LucielState))
async def test_active_is_the_only_serving_state(state):
    inst = Instance(id=uuid.uuid4(), admin_id=uuid.uuid4(), name="Acme", state=state)
    assert is_paused(inst) is (state is not LucielState.active)


async def test_every_runtime_path_shares_one_pause_predicate():
    """Three paths used to carry three copies of this rule and one of them rotted. They
    must resolve to the same function object, so the next change cannot desync them."""
    from app.api.v1 import chat_widget
    from app.channels import runtime_entry
    from app.runtime import voice_pipeline

    assert chat_widget.is_paused is is_paused
    assert voice_pipeline.is_paused is is_paused
    assert runtime_entry.is_paused is is_paused
