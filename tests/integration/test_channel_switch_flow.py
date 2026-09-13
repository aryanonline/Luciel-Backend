"""Round 6 WP-H (F060): a customer who asks for the answer on another channel gets it
there only at an address they confirmed — the channel's own, one they typed, or one
they said yes to — and the answer always stays on the channel they wrote on."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from app.channels import outbound
from app.channels.outbound import OutboundResult
from app.db.session import tenant_session
from app.models.conversation import Session as SessionModel
from app.models.instance import Instance, LucielState
from app.models.lead import LeadIdentifier
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import Orchestrator
from app.runtime.testing import EchoModel
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]


@dataclass
class _Sent:
    calls: list[dict[str, object]]
    deliver: bool = True


@pytest.fixture
def sent(monkeypatch) -> _Sent:
    box = _Sent(calls=[])

    async def fake(session, *, admin_id, instance_id, channel, to, body, subject=None, **kw):
        box.calls.append({"channel": channel, "to": to, "body": body, "subject": subject})
        return OutboundResult(
            channel=channel, delivered=box.deliver, detail=None if box.deliver else "x"
        )

    monkeypatch.setattr(outbound, "send_outbound_reply", fake)
    return box


ANSWER = "The starter package includes onboarding, two channels and email support."


async def _grounding_retriever(_q: str) -> list[str]:
    """A chunk that IS the model's fixed answer grounds it at 1.0, so every turn is an
    ANSWER (not a cannot_answer hand-off) and the channel-switch logic runs."""
    return [ANSWER]


async def _seed(s, admin, iid, *, channel="widget") -> uuid.UUID:
    s.add(
        Instance(
            id=iid,
            admin_id=admin,
            name="Aurora",
            state=LucielState.active,
            channels=[
                {"id": "widget", "enabled": True},
                {"id": "email", "enabled": True},
                {"id": "sms", "enabled": True},
            ],
            tools=[],
            escalation={},
            personality={},
        )
    )
    sid = uuid.uuid4()
    s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel=channel, mode="ai"))
    await s.flush()
    return sid


def _orch(s, admin, iid, *, reply: str = ANSWER) -> Orchestrator:
    return Orchestrator(
        session=s,
        router=LLMRouter(
            primary=EchoModel(confidence=0.9, fixed_reply=reply),
            fallback=EchoModel(confidence=0.9, fixed_reply=reply),
        ),
        retriever=_grounding_retriever,
        admin_id=admin,
        instance_id=iid,
        enabled_channels={"widget", "email", "sms"},
        has_payment_method=False,
    )


async def _turn(s, admin, iid, sid, text, *, channel="widget", transport=None, reply=ANSWER):
    return await _orch(s, admin, iid, reply=reply).run_turn(
        session_id=sid,
        resolved_lead_id=None,
        channel=channel,
        mode="ai",
        inbound_text=text,
        transport_identifier=transport,
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_anonymous_visitor_is_asked_where_then_the_answer_is_sent_and_confirmed(sent):
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed(s, admin, iid)
        first = await _turn(
            s, admin, iid, sid, "Can you email me the details of the starter package?"
        )
        assert ANSWER in first.reply_text  # the answer stays on the widget
        assert "What's the best email address to send it to?" in first.reply_text
        assert sent.calls == []
        row = (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()
        assert row.pending_outbound and row.pending_outbound["channel"] == "email"
        parked_body = row.pending_outbound["body"]

        second = await _turn(s, admin, iid, sid, "dana@lead.example")
        assert second.reply_text == "Sent — check your email at dana@lead.example."
        assert second.counted is False  # fire-once: the session was counted on turn one
        assert sent.calls == [
            {
                "channel": "email",
                "to": "dana@lead.example",
                "body": parked_body,
                "subject": "A message from your assistant",
            }
        ]
        await s.refresh(row)
        assert row.pending_outbound is None
        ident = (
            await s.execute(
                select(LeadIdentifier).where(
                    LeadIdentifier.admin_id == admin, LeadIdentifier.value == "dana@lead.example"
                )
            )
        ).scalar_one()
        assert ident.confirmed_at is not None and ident.source == "volunteered"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_address_typed_with_the_request_is_used_at_once_with_a_same_channel_note(sent):
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed(s, admin, iid)
        result = await _turn(s, admin, iid, sid, "email me the price list at dana@lead.example")
        assert result.reply_text.endswith("I've also emailed this to dana@lead.example.")
        assert ANSWER in result.reply_text
        assert len(sent.calls) == 1 and sent.calls[0]["to"] == "dana@lead.example"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_decline_drops_the_parked_answer_and_nothing_is_sent(sent):
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed(s, admin, iid)
        await _turn(s, admin, iid, sid, "Could you email me that?")
        result = await _turn(s, admin, iid, sid, "no thanks")
        assert result.reply_text == "No problem — I'll keep everything here in this conversation."
        assert sent.calls == []
        row = (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()
        assert row.pending_outbound is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_channels_own_identifier_is_confirmed_at_capture_so_a_text_needs_no_ask(sent):
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed(s, admin, iid, channel="email")
        result = await _turn(
            s,
            admin,
            iid,
            sid,
            "hi, can you text me the address?",
            channel="email",
            transport="dana@lead.example",
        )
        # No phone is known: asked, not sent. The email itself IS confirmed.
        assert "What's the best mobile number to send it to?" in result.reply_text
        assert sent.calls == []
        ident = (
            await s.execute(
                select(LeadIdentifier).where(
                    LeadIdentifier.admin_id == admin, LeadIdentifier.value == "dana@lead.example"
                )
            )
        ).scalar_one()
        assert ident.confirmed_at is not None and ident.source == "transport"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_failed_delivery_keeps_the_answer_in_the_conversation(sent):
    sent.deliver = False
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed(s, admin, iid)
        await _turn(s, admin, iid, sid, "email me the details")
        result = await _turn(s, admin, iid, sid, "yes, dana@lead.example")
        assert result.reply_text.startswith(
            "I couldn't email that just now, so here it is in full."
        )
        assert ANSWER in result.reply_text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_promise_to_send_a_copy_is_the_switch_not_an_unfounded_claim(sent):
    """Round 6 dev walk 2026-09-13 (R6-8): told that the platform delivers a copy, the live
    model wrote the answer plus "I'll have a copy sent to your email once you confirm the
    address" - and the unfounded-claim guard replaced the whole turn with the failure ack
    and flagged the team. A promise is not a claim: the switch runs, the ask goes out."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    promise = ANSWER + " I'll have a copy sent to your email once you confirm the address."
    async with tenant_session(admin) as s:
        sid = await _seed(s, admin, iid)
        first = await _turn(
            s,
            admin,
            iid,
            sid,
            "Could you email me the details of the starter package?",
            reply=promise,
        )
        assert ANSWER in first.reply_text
        assert "What's the best email address to send it to?" in first.reply_text
        assert "wasn't able to complete" not in first.reply_text
        assert first.escalation is None
        row = (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()
        assert row.pending_outbound and row.pending_outbound["channel"] == "email"
        assert sent.calls == []
