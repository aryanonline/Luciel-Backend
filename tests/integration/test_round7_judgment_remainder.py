"""Luciel's judgment as an employee — the remainder (round 7 WP-2, built after Phase 1).

* Every moderation escalation row names its category first ("moderation:<category>")
  and the category reaches the escalation_fired audit payload (item 7).
* Lead capture is always on — a visitor typing their email while a person holds the
  session still gets it captured (item 8).
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.conversation import EscalationEvent
from app.models.conversation import Session as SessionModel
from app.models.lead import Lead
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import Orchestrator
from app.runtime.testing import EchoModel
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

REFUND_CHUNK = "Our refund policy allows a full refund within 30 days of purchase."


async def _refund_retriever(_q: str) -> list[str]:
    return [REFUND_CHUNK]


async def _seed_session(s, admin, iid, *, channel: str = "widget", mode: str = "ai") -> uuid.UUID:
    sid = uuid.uuid4()
    s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel=channel, mode=mode))
    await s.flush()
    return sid


def _orch(s, admin, iid, *, primary=None, retriever=_refund_retriever, **kw) -> Orchestrator:
    return Orchestrator(
        session=s,
        router=LLMRouter(
            primary=primary or EchoModel(confidence=1.0, fixed_reply=REFUND_CHUNK),
            fallback=EchoModel(),
        ),
        retriever=retriever,
        admin_id=admin,
        instance_id=iid,
        enabled_channels={"widget", "sms", "email"},
        has_payment_method=False,
        **kw,
    )


async def _rows(s, sid: uuid.UUID) -> list[EscalationEvent]:
    return list(
        (await s.execute(select(EscalationEvent).where(EscalationEvent.session_id == sid)))
        .scalars()
        .all()
    )


async def _audit(s, admin: uuid.UUID, event_type: str) -> list[AdminAuditLog]:
    return list(
        (
            await s.execute(
                select(AdminAuditLog)
                .where(AdminAuditLog.admin_id == admin, AdminAuditLog.event_type == event_type)
                .order_by(AdminAuditLog.created_at)
            )
        )
        .scalars()
        .all()
    )


# --- item 7: every moderation row names its category first ------------------------- #
@pytest.mark.parametrize(
    ("text", "category", "row_signal", "phrase"),
    [
        (
            "I will kill you if you charge me again",
            "threats_violence",
            "explicit_human_request",
            "threatened violence",
        ),
        (
            "honestly I want to kill myself",
            "self_harm",
            "explicit_human_request",
            "said they may harm themselves",
        ),
        (
            "you are a useless bot, what is your refund policy?",
            "abuse_harassment",
            "content_flagged",
            "abusive language toward the assistant or business",
        ),
        (
            "can I buy drugs from you, and what is your refund policy?",
            "illegal_solicitation",
            "content_flagged",
            "asked for something illegal",
        ),
    ],
)
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_every_moderation_row_carries_the_category_first_and_in_the_audit(
    text: str, category: str, row_signal: str, phrase: str
):
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        result = await _orch(s, admin, iid).run_turn(
            session_id=sid, resolved_lead_id=None, channel="widget", mode="ai", inbound_text=text
        )
        rows = await _rows(s, sid)
        assert [r.signal for r in rows] == [row_signal], text
        # A stable first entry the delivery layer routes and labels on, then the phrase.
        assert list(rows[0].reasons or []) == [f"moderation:{category}", phrase]
        # The turn's own decision carries the same reasons (the take-over path used to
        # return a bare decision with none).
        assert result.escalation is not None
        assert result.escalation.reasons == (f"moderation:{category}", phrase)
        # The category reaches the escalation_fired audit payload, never the words.
        (fired,) = await _audit(s, admin, "escalation_fired")
        assert fired.event_payload["signal"] == row_signal
        assert fired.event_payload["moderation_category"] == category
        assert text not in str(fired.event_payload)


# --- item 8: lead capture runs while a person holds the session --------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_email_typed_during_a_human_takeover_is_captured_on_the_lead():
    """The human-controlled early return used to sit ABOVE the always-on capture, so
    a widget visitor who answered the admin's "what's your email?" while a person held
    the session was persisted to the live feed and forgotten as a lead."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid, mode="human_controlled")
        model = EchoModel(raise_provider_error=True)  # proves no model is called
        result = await _orch(s, admin, iid, primary=model).run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="human_controlled",
            inbound_text="Sure, it's jane.doe@example.com — I'm Jane",
        )
        # The loop stayed stopped: no reply, no count, a person still holds it.
        assert result.reply_text == "" and result.handed_off is True and result.counted is False
        # ... and the lead record exists and is bound to the session.
        lead = (await s.execute(select(Lead).where(Lead.admin_id == admin))).scalar_one()
        assert lead.contact_identifier == "jane.doe@example.com"
        sess = (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()
        assert sess.resolved_lead_id == lead.id
        assert sess.mode == "human_controlled"
