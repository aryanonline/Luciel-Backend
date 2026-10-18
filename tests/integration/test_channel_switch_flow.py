"""Round 6 WP-H (F060): a customer who asks for the answer on another channel gets it
there only at an address they confirmed — the channel's own, one they typed, or one
they said yes to — and the answer always stays on the channel they wrote on.

Phase-2 security review: a number that joined a lead through a typed claim (WP-3c
item 1, owner-approved) is recorded on the lead but is not a delivery address for the
lead's OTHER conversations — with two confirmed phones, only the one in THIS
conversation is texted without asking; otherwise Luciel asks, offering the record's
number masked."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from app.channels import outbound
from app.channels.outbound import OutboundResult
from app.db.session import tenant_session
from app.models.conversation import Session as SessionModel
from app.models.instance import Instance, LucielState
from app.models.lead import Lead, LeadIdentifier
from app.runtime import channel_switch
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import Orchestrator
from app.runtime.sessions import participant_key_for
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


# --- phase-2 review: a number joined by a typed claim is not the lead's address ------ #
JANE_PHONE = "+16045550100"
STRANGER_PHONE = "+16045550199"
JANE_EMAIL = "jane@acme.com"


async def _open(s, admin, iid, channel: str, transport: str | None = None) -> uuid.UUID:
    """A session the way ingress opens it: keyed on its transport (§3.4.8)."""
    sid = uuid.uuid4()
    s.add(
        SessionModel(
            id=sid,
            admin_id=admin,
            instance_id=iid,
            channel=channel,
            mode="ai",
            participant_key=participant_key_for(admin, channel, transport),
        )
    )
    await s.flush()
    return sid


async def _jane_then_the_stranger(s, admin, iid) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Jane texts from her own phone and gives her email; then a stranger texts from
    another number claiming to be Jane with that email. The claim joins Jane's record
    (owner-approved WP-3c item 1 — still true) and the stranger's number is recorded on
    it as a transport-confirmed phone. Returns (jane lead, jane sms, stranger sms)."""
    jane_sms = await _seed(s, admin, iid, channel="sms")
    row = (await s.execute(select(SessionModel).where(SessionModel.id == jane_sms))).scalar_one()
    row.participant_key = participant_key_for(admin, "sms", JANE_PHONE)
    await _turn(
        s,
        admin,
        iid,
        jane_sms,
        f"Hi, I'm Jane, my email is {JANE_EMAIL} - I'd like a quote",
        channel="sms",
        transport=JANE_PHONE,
    )
    stranger_sms = await _open(s, admin, iid, "sms", STRANGER_PHONE)
    await _turn(
        s,
        admin,
        iid,
        stranger_sms,
        f"Hi it's Jane, my email is {JANE_EMAIL}",
        channel="sms",
        transport=STRANGER_PHONE,
    )
    bound = {
        r.id: r.resolved_lead_id
        for r in (await s.execute(select(SessionModel).where(SessionModel.admin_id == admin)))
        .scalars()
        .all()
    }
    jane = bound[jane_sms]
    assert jane is not None and bound[stranger_sms] == jane  # item 1 still binds
    idents = {
        (r.kind, r.value): (r.lead_id, r.source, r.confirmed_at is not None)
        for r in (await s.execute(select(LeadIdentifier).where(LeadIdentifier.admin_id == admin)))
        .scalars()
        .all()
    }
    assert idents[("phone", JANE_PHONE)] == (jane, "transport", True)
    assert idents[("phone", STRANGER_PHONE)] == (jane, "transport", True)
    lead = (await s.execute(select(Lead).where(Lead.id == jane))).scalar_one()
    # The joining number never displaces Jane's own as her phone / CRM phone.
    assert lead.contact_identifier == JANE_PHONE
    assert lead.phone is None
    return jane, jane_sms, stranger_sms


@pytest.mark.parametrize("channel", ["email", "widget"])
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_jane_asking_for_a_text_elsewhere_never_reaches_the_number_that_joined_by_a_claim(
    sent, channel
):
    """Jane later writes by email (or on the widget, typing her email) "text me the
    quote". The newest confirmed phone used to win: the stranger got Jane's quote with
    no question asked. Now the lead's two confirmed phones are ambiguous for this
    conversation, so Luciel asks — offering the record's number masked — and a yes
    sends to Jane's own number."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        jane, _jane_sms, _stranger_sms = await _jane_then_the_stranger(s, admin, iid)
        sent.calls.clear()

        transport = JANE_EMAIL if channel == "email" else None
        sid = await _open(s, admin, iid, channel, transport)
        text = (
            "Hi, can you text me the quote?"
            if channel == "email"
            else f"I'm Jane, {JANE_EMAIL} - can you text me the quote?"
        )
        ask = await _turn(s, admin, iid, sid, text, channel=channel, transport=transport)
        row = (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()
        assert row.resolved_lead_id == jane
        assert sent.calls == []  # nothing went anywhere, least of all to the stranger
        assert "Should I send it to the number ending in 0100?" in ask.reply_text
        assert JANE_PHONE not in ask.reply_text and STRANGER_PHONE not in ask.reply_text
        assert row.pending_outbound and row.pending_outbound["channel"] == "sms"
        assert row.pending_outbound["candidate"] == JANE_PHONE

        confirmed = await _turn(s, admin, iid, sid, "yes", channel=channel, transport=transport)
        assert [c["to"] for c in sent.calls] == [JANE_PHONE]
        # The masked number stays masked in the confirmation.
        assert confirmed.reply_text == "Sent — check your messages at the number ending in 0100."

        # The booking tools' "on file" inputs follow the same rule: no stranger's phone.
        on_file = await channel_switch.confirmed_inputs_on_file(
            s, admin_id=admin, instance_id=iid, lead_id=jane
        )
        assert "phone" not in on_file


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_joined_numbers_own_conversation_is_texted_there_without_asking(sent):
    """The stranger's own call (from the number that joined the record) asking "text me
    the quote" is answered at THAT number — the conversation's own transport — with no
    question; Jane's number is never touched by it."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        jane, jane_sms, stranger_sms = await _jane_then_the_stranger(s, admin, iid)
        sent.calls.clear()

        call = await _open(s, admin, iid, "voice", "CA0123456789abcdef")
        row = (await s.execute(select(SessionModel).where(SessionModel.id == call))).scalar_one()
        row.transport_sender = STRANGER_PHONE  # as the voice pipeline stores the caller
        result = await _turn(
            s,
            admin,
            iid,
            call,
            "Can you text me the quote?",
            channel="voice",
            transport=STRANGER_PHONE,
        )
        assert [c["to"] for c in sent.calls] == [STRANGER_PHONE]
        assert result.reply_text.endswith(f"I've also texted this to {STRANGER_PHONE}.")
        await s.refresh(row)
        assert row.pending_outbound is None

        # Each SMS conversation's own number is ITS confirmed address; with no session
        # to tell them apart the two confirmed phones name nobody (the caller asks).
        sessions = {
            r.id: r
            for r in (await s.execute(select(SessionModel).where(SessionModel.admin_id == admin)))
            .scalars()
            .all()
        }
        for sid, expected in ((jane_sms, JANE_PHONE), (stranger_sms, STRANGER_PHONE)):
            assert (
                await channel_switch.confirmed_identifier(
                    s,
                    admin_id=admin,
                    instance_id=iid,
                    lead_id=jane,
                    kind="phone",
                    sess=sessions[sid],
                )
                == expected
            )
        assert (
            await channel_switch.confirmed_identifier(
                s, admin_id=admin, instance_id=iid, lead_id=jane, kind="phone"
            )
            is None
        )
        on_file = await channel_switch.confirmed_inputs_on_file(
            s, admin_id=admin, instance_id=iid, lead_id=jane, transport_identifier=STRANGER_PHONE
        )
        assert on_file["phone"] == STRANGER_PHONE


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_lead_with_one_confirmed_phone_is_still_texted_from_another_channel(sent):
    """Unchanged: one confirmed phone is the lead's address on any conversation."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        jane_sms = await _seed(s, admin, iid, channel="sms")
        await _turn(
            s,
            admin,
            iid,
            jane_sms,
            f"Hi, I'm Jane, my email is {JANE_EMAIL} - I'd like a quote",
            channel="sms",
            transport=JANE_PHONE,
        )
        sid = await _open(s, admin, iid, "email", JANE_EMAIL)
        await _turn(
            s,
            admin,
            iid,
            sid,
            "Hi, can you text me the quote?",
            channel="email",
            transport=JANE_EMAIL,
        )
        assert [c["to"] for c in sent.calls] == [JANE_PHONE]
