"""schedule_callback opt-out (Architecture §3.3.5): an opted-out lead is NEVER sent a
scheduled callback, regardless of caps — enforced both at ScheduleCallbackTool.execute
(scheduling time) and in the deferred_queue delivery worker (fire time, in case the
lead opts out AFTER a callback was already queued).

Lead.opted_out is added by the core owner (migration 0017) — this test only reads it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.connections import deferred_queue
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from app.models.deferred import DeferredAction
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.lead import Lead
from app.models.tool_auth import InstanceToolAuthorization
from app.tools import catalog
from app.tools.broker import ToolBroker
from app.tools.contract import Outcome
from sqlalchemy import select


async def _seed(admin, *, authorize, opted_out: bool) -> tuple[uuid.UUID, uuid.UUID]:
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@x.com",
                email_lower=f"{admin}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        iid = uuid.uuid4()
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="L",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        for tool_id in authorize:
            s.add(
                InstanceToolAuthorization(
                    id=uuid.uuid4(), admin_id=admin, instance_id=iid, tool_id=tool_id, enabled=True
                )
            )
        lead_id = uuid.uuid4()
        s.add(
            Lead(
                id=lead_id,
                admin_id=admin,
                instance_id=iid,
                name="Jane",
                contact_identifier="jane@x.com",
                opted_out=opted_out,
            )
        )
        await s.flush()
        return iid, lead_id


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_schedule_callback_refuses_opted_out_lead_regardless_of_caps():
    """§3.3.5: an opted-out lead's schedule_callback is refused even though it is
    nowhere near any rate-limit cap, and NO DeferredAction row is written."""
    admin = uuid.uuid4()
    iid, lead_id = await _seed(admin, authorize=["schedule_callback"], opted_out=True)
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        res = await broker.dispatch(
            catalog.ScheduleCallbackTool(),
            {"fire_at": "2026-06-25T10:00Z", "channel": "email", "message": "follow up"},
            admin_id=admin,
            instance_id=iid,
            resolved_lead_id=lead_id,
        )
        assert res.outcome is Outcome.failed
        assert res.reason == "opted_out"

        count = (
            (
                await s.execute(
                    select(DeferredAction).where(DeferredAction.resolved_lead_id == lead_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(count) == 0

        # The refusal is traced (§3.3.4 rule 3).
        last = broker.traces[-1]
        assert last["outcome"] == "failed" and last["reason"] == "opted_out"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_schedule_callback_allows_non_opted_out_lead():
    """Control case: a lead that has NOT opted out is scheduled normally."""
    admin = uuid.uuid4()
    iid, lead_id = await _seed(admin, authorize=["schedule_callback"], opted_out=False)
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        res = await broker.dispatch(
            catalog.ScheduleCallbackTool(),
            {"fire_at": "2026-06-25T10:00Z", "channel": "email", "message": "follow up"},
            admin_id=admin,
            instance_id=iid,
            resolved_lead_id=lead_id,
        )
        assert res.outcome is Outcome.ok


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_deferred_sweep_abandons_queued_callback_for_opted_out_lead():
    """§3.3.5: if a lead opts out AFTER a callback was already queued, the delivery
    worker (run_sweep) must skip/abandon it rather than deliver it — even though the
    connection is healthy and it is otherwise due."""

    class _AlwaysDeliver:
        async def deliver(self, action: DeferredAction) -> bool:
            return True

    admin = uuid.uuid4()
    iid, lead_id = await _seed(admin, authorize=["schedule_callback"], opted_out=False)
    now = datetime.now(UTC)
    async with tenant_session(admin) as s:
        action = await deferred_queue.enqueue(
            s,
            admin_id=admin,
            instance_id=iid,
            action_type="schedule_callback",
            connection_type="email_sender",
            resolved_lead_id=lead_id,
            payload={"fire_at": "2026-06-25T10:00Z", "channel": "email", "message": "m"},
            now=now,
        )
        # Lead opts out AFTER the callback was queued.
        lead = (await s.execute(select(Lead).where(Lead.id == lead_id))).scalar_one()
        lead.opted_out = True
        await s.flush()

        counts = await deferred_queue.run_sweep(
            s, admin_id=admin, deliverer=_AlwaysDeliver(), now=now + timedelta(minutes=1)
        )
        assert counts["abandoned"] == 1
        assert counts["delivered"] == 0

        await s.refresh(action)
        assert action.status == "abandoned"
        assert action.last_error == "opted_out"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_deferred_sweep_delivers_normally_when_lead_not_opted_out():
    """Control case: run_sweep delivers a queued callback normally when the lead has
    not opted out (proves the new check doesn't regress the happy path)."""

    class _AlwaysDeliver:
        async def deliver(self, action: DeferredAction) -> bool:
            return True

    admin = uuid.uuid4()
    iid, lead_id = await _seed(admin, authorize=["schedule_callback"], opted_out=False)
    now = datetime.now(UTC)
    async with tenant_session(admin) as s:
        # A connected email_sender so the health gate lets delivery proceed.
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="email_sender",
                auth_class="api_key",
                provider="ses",
                status="connected",
            )
        )
        await deferred_queue.enqueue(
            s,
            admin_id=admin,
            instance_id=iid,
            action_type="schedule_callback",
            connection_type="email_sender",
            resolved_lead_id=lead_id,
            payload={"fire_at": "2026-06-25T10:00Z", "channel": "email", "message": "m"},
            now=now,
        )
        counts = await deferred_queue.run_sweep(
            s, admin_id=admin, deliverer=_AlwaysDeliver(), now=now + timedelta(minutes=1)
        )
        assert counts["delivered"] == 1
        assert counts["abandoned"] == 0
