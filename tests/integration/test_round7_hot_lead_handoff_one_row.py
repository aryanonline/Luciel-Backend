"""A hot lead Luciel cannot answer is ONE row and ONE page (live SeedTest M3, 2026-10-25).

The live matrix case M3 asked, in one widget turn, a question with a budget and "this
week" in it (gate 2 fires ``high_value_lead``) that the knowledge cannot answer — and the
live model called ``hand_to_team``, which writes the ``luciel_requested_handoff`` row and
queues the owner's page at dispatch. Gate 2 then wrote a SECOND row, ``high_value_lead``,
with a second page: the owner was paged twice for one message.

The contract (round 7 WP-2, item 1): one row for the turn, labelled by the signal that
won it (``high_value_lead`` beats the hand-off), carrying BOTH reason sets and Luciel's
own reason, and one page — the hand-off's queued page re-keyed to the winner.

Driven through the real loop with a deterministic model double that calls the tool the
way the live model did; nothing is faked below the loop except the SES client.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.conversation import EscalationEvent
from app.models.conversation import Session as SessionModel
from app.models.email_incident import EmailDeliveryIncident
from app.models.instance import Account, AccountState, Instance, LucielState
from app.notifications import email_notifier, escalation_dispatch
from app.runtime import orchestrator as orch_mod
from app.runtime.escalation import Signal
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import _HANDOFF_ACK_MARK, Orchestrator
from app.runtime.testing import EchoModel
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

OWNER_EMAIL = "owner@drones.example"
CONTACT = "sales@drones.example"
#: The SeedTest M3 message (scripts/seed_and_livetest.py HOT_UNGROUNDED_QUESTION).
HOT_UNGROUNDED = (
    "We have a $5,000 budget and want to buy this week - what is the exact battery "
    "capacity, in mAh, of the refurbished DJI Mavic 3 Pro drones you sell?"
)
MODEL_REASON = "asked for a battery spec the knowledge does not carry"
DRAFT = "I don't have that figure, so I've passed your question to the team."
HANDOFF_REASON = "Luciel asked for a person"


async def _unrelated_retriever(_q: str) -> list[str]:
    # Nothing about batteries: the draft lands below the grounding floor.
    return ["Our showroom is open Saturdays from 9 to 3 and closed on Sundays."]


def _hands_to_team() -> EchoModel:
    """The live model's shape: calls hand_to_team, reads the result back, then says so
    in words the knowledge cannot back."""
    return EchoModel(
        confidence=1.0,
        tool_call_script=[[{"tool_id": "hand_to_team", "inputs": {"reason": MODEL_REASON}}]],
        fixed_reply=DRAFT,
    )


def _orch(s, admin: uuid.UUID, iid: uuid.UUID) -> Orchestrator:
    return Orchestrator(
        session=s,
        router=LLMRouter(primary=_hands_to_team(), fallback=EchoModel()),
        retriever=_unrelated_retriever,
        admin_id=admin,
        instance_id=iid,
        enabled_channels={"widget"},
        has_payment_method=False,
    )


async def _turn(orch: Orchestrator, sid: uuid.UUID):
    return await orch.run_turn(
        session_id=sid,
        resolved_lead_id=None,
        channel="widget",
        mode="ai",
        inbound_text=HOT_UNGROUNDED,
        received_at=datetime.now(UTC),
    )


async def _rows(s, sid: uuid.UUID) -> list[EscalationEvent]:
    return list(
        (
            await s.execute(
                select(EscalationEvent)
                .where(EscalationEvent.session_id == sid)
                .order_by(EscalationEvent.created_at)
            )
        )
        .scalars()
        .all()
    )


def _queued_signals(s) -> list[str]:
    """The owner pages this turn's transaction will start at commit."""
    queued = s.sync_session.info.get(escalation_dispatch._AFTER_COMMIT_QUEUE) or []
    return [str(kwargs["signal"]) for _loop, kwargs in queued]


class _RecordingEmailSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, *, to, subject, body_text, from_address=None, tags=None):
        self.sent.append((to, subject, body_text))
        return f"ses-{len(self.sent)}"


@pytest.fixture
def ses(monkeypatch) -> _RecordingEmailSender:
    fake = _RecordingEmailSender()
    monkeypatch.setattr(email_notifier, "build_email_sender", lambda: fake)
    return fake


async def _seed_tenant(admin: uuid.UUID, iid: uuid.UUID) -> uuid.UUID:
    """An owner, a Luciel paging a verified email contact, and an open widget session."""
    sid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            EmailDeliveryIncident(
                admin_id=admin, email_lower=CONTACT, verified_at=datetime.now(UTC)
            )
        )
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=uuid.uuid4(),
                email=OWNER_EMAIL,
                email_lower=OWNER_EMAIL,
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="Sky Drones",
                state=LucielState.active,
                escalation={"primaryEmail": CONTACT, "preferredChannel": "email"},
            )
        )
        s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel="widget", mode="ai"))
        await s.flush()
    return sid


async def _audit(admin: uuid.UUID, event_type: str) -> list[AdminAuditLog]:
    async with tenant_session(admin) as s:
        return list(
            (
                await s.execute(
                    select(AdminAuditLog)
                    .where(AdminAuditLog.admin_id == admin, AdminAuditLog.event_type == event_type)
                    .order_by(AdminAuditLog.seq.asc())
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_hot_lead_luciel_hands_to_the_team_is_one_high_value_row_and_one_page(ses):
    admin, iid = uuid.uuid4(), uuid.uuid4()
    sid = await _seed_tenant(admin, iid)

    async with tenant_session(admin) as s:
        result = await _turn(_orch(s, admin, iid), sid)

        # The live condition: the tool recorded its row at dispatch AND gate 2 fired
        # high_value_lead on an ungrounded draft.
        assert result.tool_outcomes == ["ok"]
        assert result.escalation is not None
        assert result.escalation.signal is Signal.high_value_lead
        assert result.escalation.answer_ungrounded is True
        assert result.escalation.score_or_confidence is not None
        assert result.escalation.score_or_confidence >= 0.5

        # ONE row for the session, labelled by the winner, with every reason on it.
        rows = await _rows(s, sid)
        assert [(r.signal, r.gate) for r in rows] == [("high_value_lead", "outcome")]
        row = rows[0]
        reasons = [str(r) for r in (row.reasons or [])]
        assert "named a budget" in reasons and "wants it soon" in reasons  # the lead half
        assert any("backed by your knowledge" in r for r in reasons)  # the answer half
        assert HANDOFF_REASON in reasons and MODEL_REASON in reasons  # Luciel's own
        assert len(reasons) == len(set(reasons))
        assert tuple(reasons) == result.escalation.reasons  # the turn reports the row
        assert row.score_or_confidence == result.escalation.score_or_confidence
        assert row.delivery_status == "pending"

        # ONE page queued, re-keyed to the row's label; nothing starts before commit.
        assert _queued_signals(s) == ["high_value_lead"]
        assert ses.sent == []

        # The below-floor draft never leaves; the honest hand-off does.
        assert DRAFT not in result.reply_text
        assert _HANDOFF_ACK_MARK.search(result.reply_text)

    await escalation_dispatch.drain_background_notifications()

    # Exactly one delivery, for the high_value_lead row, carrying both halves.
    assert [to for to, _subject, _body in ses.sent] == [CONTACT]
    _to, subject, body = ses.sent[0]
    assert "high-value lead" in subject
    assert "named a budget" in body and "backed by your knowledge" in body
    assert MODEL_REASON in body
    sent = await _audit(admin, "escalation_notification_sent")
    assert [(a.event_payload["signal"], a.event_payload["recipient_role"]) for a in sent] == [
        ("high_value_lead", "primary")
    ]
    assert await _audit(admin, "escalation_delivery_failed") == []
    async with tenant_session(admin) as s:
        (row,) = await _rows(s, sid)
        assert row.signal == "high_value_lead"
        assert row.delivery_status == "delivered"
        assert row.delivery_detail == "email:primary"


@pytest.fixture
def paged(monkeypatch) -> list[str]:
    """The signal of every owner page started after the turn's commit."""
    signals: list[str] = []

    async def fake_notify(session, **kw: Any) -> None:
        signals.append(str(kw["signal"]))

    monkeypatch.setattr(escalation_dispatch, "notify_escalation_safely", fake_notify)
    return signals


@pytest.mark.parametrize("cause", ["page_already_started", "racing_turn_wrote_the_lead_row"])
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_row_that_cannot_be_relabelled_carries_the_lead_and_pages_nobody_twice(
    monkeypatch, paged: list[str], cause: str
):
    """The two ways the hand-off row cannot become the winner's row: its page is no
    longer queued (it already started — relabelling under it would orphan its delivery
    stamp), or a racing turn wrote this session's high_value_lead row after the
    suppression check. Either way the turn writes no second row and schedules no second
    page: the lead reasons ride on the hand-off row, as the answer reasons always did."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    sid = await _seed_tenant(admin, iid)
    if cause == "page_already_started":
        monkeypatch.setattr(orch_mod, "relabel_scheduled_notification", lambda *a, **k: False)
    else:
        async with tenant_session(admin) as s:
            s.add(
                EscalationEvent(
                    admin_id=admin,
                    instance_id=iid,
                    session_id=sid,
                    signal="high_value_lead",
                    gate="outcome",
                    score_or_confidence=0.8,
                    reasons=["named a budget"],
                )
            )
        real_check = orch_mod._session_already_escalated

        async def raced_check(session, *, admin_id, session_id, signal):
            if signal == "high_value_lead":
                return False  # this turn checked before the racing turn committed
            return await real_check(
                session, admin_id=admin_id, session_id=session_id, signal=signal
            )

        monkeypatch.setattr(orch_mod, "_session_already_escalated", raced_check)

    async with tenant_session(admin) as s:
        result = await _turn(_orch(s, admin, iid), sid)
        assert result.escalation is not None
        assert result.escalation.signal is Signal.luciel_requested_handoff
        assert result.escalation.answer_ungrounded is True
        rows = {r.signal: r for r in await _rows(s, sid)}
        expected = ["luciel_requested_handoff"]
        if cause == "racing_turn_wrote_the_lead_row":
            expected.append("high_value_lead")
            assert rows["high_value_lead"].reasons == ["named a budget"]  # untouched
        assert sorted(rows) == sorted(expected)
        handoff = [str(r) for r in (rows["luciel_requested_handoff"].reasons or [])]
        assert handoff[:2] == [HANDOFF_REASON, MODEL_REASON]
        assert "wants it soon" in handoff
        assert any("backed by your knowledge" in r for r in handoff)
        assert _queued_signals(s) == ["luciel_requested_handoff"]
        assert DRAFT not in result.reply_text
    await escalation_dispatch.drain_background_notifications()
    assert paged == ["luciel_requested_handoff"]
