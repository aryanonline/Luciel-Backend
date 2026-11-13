"""Round 6 WP-E (F054): team availability changes the PROMISE about a human, never the
conversation. The loop answers identically whether the team is reachable or not; only
the hand-off line says WHEN a person follows up, and only while nobody is reachable."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.db.session import tenant_session
from app.models.conversation import Session as SessionModel
from app.models.instance import Instance, LucielState
from app.runtime import orchestrator as orchestrator_module
from app.runtime import team_availability
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import Orchestrator
from app.runtime.testing import EchoModel

WEEKDAYS_9_TO_5 = {
    "enabled": True,
    "weekly": [{"day": d, "start": "09:00", "end": "17:00"} for d in range(5)],
    "afterHours": {"escalation": {"channel": "sms", "contactSms": "+16045559999"}},
}
OFFICE_TIME = datetime(2026, 9, 8, 14, 30, tzinfo=UTC)  # Tue 10:30 EDT
NIGHT_TIME = datetime(2026, 9, 9, 3, 0, tzinfo=UTC)  # Tue 23:00 EDT


async def _empty_retriever(_q: str) -> list[str]:
    return []


async def _seed(s, admin, iid, *, hours, escalation) -> uuid.UUID:
    s.add(
        Instance(
            id=iid,
            admin_id=admin,
            name="Acme",
            state=LucielState.active,
            channels=[],
            tools=[],
            escalation=escalation,
            personality={},
            timezone="America/Toronto",
            team_availability=hours,
        )
    )
    sid = uuid.uuid4()
    s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel="widget", mode="ai"))
    await s.flush()
    return sid


def _orch(s, admin, iid) -> Orchestrator:
    return Orchestrator(
        session=s,
        router=LLMRouter(primary=EchoModel(), fallback=EchoModel()),
        retriever=_empty_retriever,
        admin_id=admin,
        instance_id=iid,
        enabled_channels={"widget"},
        has_payment_method=False,
    )


async def _run(monkeypatch, *, now, hours, escalation, text="I want to speak to a human please"):
    # The orchestrator imported follow_up_phrase by name; pin the clock it evaluates at.
    real = team_availability.follow_up_phrase
    monkeypatch.setattr(orchestrator_module, "follow_up_phrase", lambda ta: real(ta, now=now))
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed(s, admin, iid, hours=hours, escalation=escalation)
        return await _orch(s, admin, iid).run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text=text,
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_after_hours_handoff_names_when_a_person_follows_up(monkeypatch):
    result = await _run(
        monkeypatch,
        now=NIGHT_TIME,
        hours=WEEKDAYS_9_TO_5,
        escalation={"primaryEmail": "office@acme.example"},
    )
    assert result.handed_off is True
    text = result.reply_text
    assert "flagged this for the team" in text
    assert "tomorrow at 9:00 am (EDT)" in text
    assert "shortly" not in text
    # The doctrine still holds: no real-time claim, and the anonymous visitor is asked
    # how the team can reach them.
    assert "right now, so someone will follow up" in text
    assert "connecting" not in text.lower()
    assert "what's your name" in text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_office_hours_handoff_is_word_for_word_what_it_was(monkeypatch):
    result = await _run(
        monkeypatch,
        now=OFFICE_TIME,
        hours=WEEKDAYS_9_TO_5,
        escalation={"primaryEmail": "office@acme.example"},
    )
    assert result.handed_off is True
    assert "they'll follow up with you shortly" in result.reply_text
    assert "away right now" not in result.reply_text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_setting_off_keeps_the_generic_line_even_at_night(monkeypatch):
    result = await _run(
        monkeypatch,
        now=NIGHT_TIME,
        hours={**WEEKDAYS_9_TO_5, "enabled": False},
        escalation={},
    )
    assert result.handed_off is True
    assert "passed this along to the team" in result.reply_text
    assert "follow up with you shortly" in result.reply_text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_normal_question_is_answered_the_same_day_or_night(monkeypatch):
    """Luciel has no hours: an ordinary turn at 11 pm is served exactly like one at
    10 am — same disclosure, same echoed answer, counted once."""
    day = await _run(
        monkeypatch,
        now=OFFICE_TIME,
        hours=WEEKDAYS_9_TO_5,
        escalation={},
        text="Do you have any openings?",
    )
    night = await _run(
        monkeypatch,
        now=NIGHT_TIME,
        hours=WEEKDAYS_9_TO_5,
        escalation={},
        text="Do you have any openings?",
    )
    assert day.handed_off is False and night.handed_off is False
    assert day.reply_text == night.reply_text
    assert "Echo:" in night.reply_text
    assert night.counted is True
