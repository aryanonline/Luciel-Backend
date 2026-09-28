"""Judgment as an employee, the remainder (round 7 WP-2): the loop through the real
orchestrator with the deterministic EchoModel.

* Item 1: an anonymous widget visitor whose three questions the knowledge cannot
  cover hears the full hand-off line and the contact ask ONCE, then two short, varied
  acknowledgements; the session keeps one escalation row, and the owner is paged once
  per DISTINCT uncovered question (the new question rides on the row's reasons) —
  never for a repeat.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.db.session import tenant_session
from app.models.conversation import EscalationEvent
from app.models.conversation import Session as SessionModel
from app.models.instance import Instance, LucielState
from app.notifications import email_notifier, escalation_dispatch
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import (
    CONTACT_ASK_LINE,
    SHORT_HANDOFF_ACKS,
    Orchestrator,
    handoff_ack_sent,
)
from app.runtime.testing import EchoModel
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

CONTACT = "frontdesk@brightsmile.example"
# A statement, not a question: the model's draft is a guess with no knowledge behind it,
# so the below-floor replacement runs (a bare clarifying question would ship as-is, F100).
GUESS = "We do those from $900 and there is parking behind the building."


class _Sender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, *, to, subject, body_text, from_address=None, tags=None):
        self.sent.append(to)
        return f"ses-{len(self.sent)}"


@pytest.fixture
def ses(monkeypatch) -> _Sender:
    fake = _Sender()
    monkeypatch.setattr(email_notifier, "build_email_sender", lambda: fake)
    return fake


async def _empty_retriever(_q: str) -> list[str]:
    return []


async def _seed(s, admin: uuid.UUID, iid: uuid.UUID) -> uuid.UUID:
    s.add(
        Instance(
            id=iid,
            admin_id=admin,
            name="Bright Smile Dental",
            state=LucielState.active,
            escalation={"primaryEmail": CONTACT, "preferredChannel": "email"},
        )
    )
    sid = uuid.uuid4()
    s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel="widget", mode="ai"))
    await s.flush()
    return sid


def _orch(s, admin: uuid.UUID, iid: uuid.UUID) -> Orchestrator:
    primary = EchoModel(confidence=1.0, fixed_reply=GUESS)
    return Orchestrator(
        session=s,
        router=LLMRouter(primary=primary, fallback=primary),
        retriever=_empty_retriever,
        admin_id=admin,
        instance_id=iid,
        enabled_channels={"widget"},
        has_payment_method=False,
    )


async def _turns(s, orch: Orchestrator, sid: uuid.UUID, questions: list[str]) -> list[str]:
    replies: list[str] = []
    for text in questions:
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text=text,
            # The adapters stamp the inbound's TRUE arrival time; without it every lead
            # row of one transaction shares the transaction timestamp and the transcript
            # order the acks are read from collapses.
            received_at=datetime.now(UTC),
        )
        assert result.escalation is not None and result.handed_off is False
        replies.append(result.reply_text)
    return replies


async def _rows(s, sid: uuid.UUID) -> list[EscalationEvent]:
    return list(
        (await s.execute(select(EscalationEvent).where(EscalationEvent.session_id == sid)))
        .scalars()
        .all()
    )


# --- item 1 ------------------------------------------------------------------------ #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_three_uncovered_questions_get_one_full_ack_then_short_ones(ses: _Sender):
    """The full hand-off paragraph and the contact ask used to go out on EVERY
    below-floor turn — the visitor read the identical block three times, and the
    second and third questions never reached the owner because the row was already
    written. Now: one full ack + one ask, two short varied acks, one row carrying
    every question, one page per distinct question."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    questions = ["do you do implants?", "how much are crowns?", "do you open saturdays?"]
    async with tenant_session(admin) as s:
        sid = await _seed(s, admin, iid)
        first, second, third = await _turns(s, _orch(s, admin, iid), sid, questions)

        # Turn 1: the full ack and the contact ask (the anonymous visitor is unreachable).
        assert handoff_ack_sent([SimpleNamespace(role="luciel", text=first)])
        assert CONTACT_ASK_LINE in first
        assert GUESS not in first
        # Turns 2 and 3: short, different, and no nag for contact details.
        assert second.startswith(SHORT_HANDOFF_ACKS[0])
        assert third.startswith(SHORT_HANDOFF_ACKS[1])
        for later in (second, third):
            assert CONTACT_ASK_LINE not in later
            assert "flagged this for the team" not in later
            assert GUESS not in later
        assert len({first, second, third}) == 3  # never the same paragraph three times

        # ONE row for the session; every later distinct question is on its reasons.
        (row,) = await _rows(s, sid)
        assert row.signal == "cannot_answer"
        reasons = list(row.reasons or [])
        assert 'also asked: "how much are crowns?"' in reasons
        assert 'also asked: "do you open saturdays?"' in reasons

    # Delivery runs after the commit, off the customer's turn: one page for the row,
    # one re-notification per distinct question the visitor went on to ask.
    await escalation_dispatch.drain_background_notifications()
    assert ses.sent == [CONTACT, CONTACT, CONTACT]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_repeated_uncovered_question_pages_nobody_again(ses: _Sender):
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed(s, admin, iid)
        first, second = await _turns(
            s, _orch(s, admin, iid), sid, ["do you do implants?", "implants??"]
        )
        assert CONTACT_ASK_LINE in first and CONTACT_ASK_LINE not in second
        assert second.startswith(SHORT_HANDOFF_ACKS[0])  # still a short ack, not silence
        (row,) = await _rows(s, sid)
        assert not any(str(r).startswith("also asked") for r in row.reasons or [])
    await escalation_dispatch.drain_background_notifications()
    assert ses.sent == [CONTACT]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_contact_ask_is_not_repeated_while_the_visitor_stays_anonymous(ses: _Sender):
    """Five uncovered questions in a row: the ask goes out on the first and — with
    still nothing on file — once more after the respite, never on every turn."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    questions = [
        "do you do implants?",
        "how much are crowns?",
        "do you open saturdays?",
        "is parking free?",
        "do you take insurance?",
        "can kids come too?",
    ]
    async with tenant_session(admin) as s:
        sid = await _seed(s, admin, iid)
        replies = await _turns(s, _orch(s, admin, iid), sid, questions)
    asked = [CONTACT_ASK_LINE in r for r in replies]
    assert asked == [True, False, False, False, False, True]
