"""Human handoff / live takeover cognition (Architecture §3.4.12)."""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.conversation import Session as SessionModel
from app.runtime import budget, handoff
from app.runtime.handoff import ConversationMode

pytestmark = [pytest.mark.asyncio]


def test_loop_runs_only_in_ai_mode():
    # human_controlled STOPS the loop (§3.4.12).
    assert handoff.loop_should_run("ai") is True
    assert handoff.loop_should_run("human_controlled") is False


async def _seed_session(s, admin, iid) -> uuid.UUID:
    sid = uuid.uuid4()
    s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel="widget", mode="ai"))
    await s.flush()
    return sid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_take_over_sets_human_controlled_and_counts_once():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        sess = await handoff.take_over(
            s,
            admin_id=admin,
            instance_id=iid,
            session_id=sid,
            actor_user_id=admin,
        )
        assert sess.mode == ConversationMode.human_controlled.value
        # Becoming human-controlled is the fire-once budget trigger (§3.4.1b / §7 #33).
        state = await budget.check_budget(s, admin_id=admin, instance_id=iid)
        assert state.counted == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_take_over_is_idempotent_on_budget():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        await handoff.take_over(
            s, admin_id=admin, instance_id=iid, session_id=sid, actor_user_id=admin
        )
        await handoff.take_over(
            s, admin_id=admin, instance_id=iid, session_id=sid, actor_user_id=admin
        )
        state = await budget.check_budget(s, admin_id=admin, instance_id=iid)
        assert state.counted == 1  # still one — same session


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_hand_back_returns_to_ai():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        await handoff.take_over(
            s, admin_id=admin, instance_id=iid, session_id=sid, actor_user_id=admin
        )
        sess = await handoff.hand_back(
            s, admin_id=admin, instance_id=iid, session_id=sid, actor_user_id=admin
        )
        assert sess.mode == ConversationMode.ai.value


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_take_over_missing_session_raises():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        with pytest.raises(LookupError):
            await handoff.take_over(
                s,
                admin_id=admin,
                instance_id=iid,
                session_id=uuid.uuid4(),
                actor_user_id=admin,
            )
