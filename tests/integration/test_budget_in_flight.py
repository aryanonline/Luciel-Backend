"""The conversation cap gates NEW conversations, never one already in flight
(Legal §A3 #3 — in-flight conversations are honored at reduction; round 7 WP-1 #1).

The gate used to read the period counter alone, so the turn that counted the 50th
free conversation pushed the counter to 50 and the SAME conversation's next turn got
the at-cap reply — the 50th customer was cut mid-sentence. A card removal or a day-7
dunning reduction did the same to every live chat. A session that already holds a
row in the fire-once ledger is one the tenant was counted (and, past 50, billed)
for; it is served to its end. A conversation the ledger has never seen still gets
the at-cap reply, which still escalates and never reaches the model.

Deterministic EchoModel, no network.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.config import get_settings
from app.db.session import tenant_session
from app.models.conversation import EscalationEvent
from app.models.conversation import Session as SessionModel
from app.runtime import budget
from app.runtime.escalation import Signal
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import AT_CAP_REPLY, LoopResult, Orchestrator
from app.runtime.testing import EchoModel
from sqlalchemy import select


class CountingEcho(EchoModel):
    """EchoModel that counts how many times the loop actually reached the model."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.calls = 0

    async def complete(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return await super().complete(*args, **kwargs)


async def _empty_retriever(_q: str) -> list[str]:
    return []


async def _open_session(admin: uuid.UUID, iid: uuid.UUID) -> uuid.UUID:
    sid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel="widget", mode="ai"))
    return sid


async def _turn(
    admin: uuid.UUID, iid: uuid.UUID, sid: uuid.UUID, model: CountingEcho, *, has_pm: bool
) -> LoopResult:
    """One inbound turn in its own transaction — a request, not a test-long session."""
    async with tenant_session(admin) as s:
        orch = Orchestrator(
            session=s,
            router=LLMRouter(primary=model, fallback=model),
            retriever=_empty_retriever,
            admin_id=admin,
            instance_id=iid,
            enabled_channels={"widget"},
            has_payment_method=has_pm,
        )
        return await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="Do you have any openings this week?",
        )


async def _counted(admin: uuid.UUID, iid: uuid.UUID) -> int:
    async with tenant_session(admin) as s:
        return (await budget.check_budget(s, admin_id=admin, instance_id=iid)).counted


async def _fill_to(admin: uuid.UUID, iid: uuid.UUID, n: int) -> None:
    async with tenant_session(admin) as s:
        for _ in range(n):
            await budget.count_session_once(
                s, admin_id=admin, instance_id=iid, session_id=uuid.uuid4()
            )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_fiftieth_free_conversation_is_served_to_its_end_and_the_next_is_capped():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    model = CountingEcho(confidence=0.9)
    free = get_settings().free_allowance
    await _fill_to(admin, iid, free - 1)

    # The 50th conversation: its first turn is served and counts it.
    in_flight = await _open_session(admin, iid)
    first = await _turn(admin, iid, in_flight, model, has_pm=False)
    assert first.at_cap is False and first.counted is True
    assert model.calls == 1
    assert await _counted(admin, iid) == free  # the cap is reached NOW

    # Its second turn is still a served turn — same conversation, already counted.
    second = await _turn(admin, iid, in_flight, model, has_pm=False)
    assert second.at_cap is False, "an in-flight conversation was cut at the cap"
    assert not second.reply_text.startswith(AT_CAP_REPLY)
    assert model.calls == 2  # the model answered it
    assert second.counted is False  # ...and it was not counted a second time
    assert await _counted(admin, iid) == free

    # A NEW conversation is what the cap is for: graceful reply, escalation, no model.
    newcomer = await _open_session(admin, iid)
    capped = await _turn(admin, iid, newcomer, model, has_pm=False)
    assert capped.at_cap is True
    assert capped.reply_text.startswith(AT_CAP_REPLY)
    assert capped.counted is False
    assert capped.escalation is not None and capped.escalation.signal is Signal.budget_exhausted
    assert model.calls == 2, "the at-cap reply must never reach the model"
    assert await _counted(admin, iid) == free  # the capped turn did not count

    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(EscalationEvent).where(
                        EscalationEvent.admin_id == admin,
                        EscalationEvent.signal == "budget_exhausted",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [r.session_id for r in rows] == [newcomer]  # escalated once, for the newcomer only


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_card_removed_mid_conversation_does_not_sever_the_live_chat():
    """A paying tenant past the free 50 removes their card (or dunning reduces them)
    while a customer is mid-chat: that chat finishes; the next customer is capped."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    model = CountingEcho(confidence=0.9)
    free = get_settings().free_allowance
    await _fill_to(admin, iid, free + 10)  # well past the allowance, on PAYG

    live_chat = await _open_session(admin, iid)
    served = await _turn(admin, iid, live_chat, model, has_pm=True)
    assert served.at_cap is False and served.counted is True
    assert await _counted(admin, iid) == free + 11

    # The card goes away between two turns of the same conversation.
    after_removal = await _turn(admin, iid, live_chat, model, has_pm=False)
    assert after_removal.at_cap is False
    assert model.calls == 2

    newcomer = await _open_session(admin, iid)
    capped = await _turn(admin, iid, newcomer, model, has_pm=False)
    assert capped.at_cap is True and model.calls == 2
