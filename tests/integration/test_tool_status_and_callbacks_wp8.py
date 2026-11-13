"""Tools tell the truth about their connection; callbacks only where they can land
(2026-09-05 audit WP8-C, F151 / F144 / F175)."""

from __future__ import annotations

import uuid

import pytest
from app.api.v1.admin import luciel as luciel_api
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from app.models.instance import Instance, LucielState
from app.tools import contract as c
from app.tools.catalog import ScheduleCallbackTool
from app.tools.contract import Outcome, ToolContext


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_tools_carry_their_live_connection_status():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        inst = Instance(
            id=iid,
            admin_id=admin,
            name="T",
            state=LucielState.active,
            channels=[{"id": "sms", "enabled": True}],
            tools=[
                {"id": "send_sms", "enabled": True},
                {"id": "send_email", "enabled": True},
                {"id": "schedule_callback", "enabled": True},
            ],
            escalation={},
            personality={},
        )
        s.add(inst)
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="sms_sender",
                provider="twilio",
                auth_class="long_lived_token",
                status="connected",
            )
        )
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="email_sender",
                provider="outlook",
                auth_class="oauth_token",
                status="expired",
            )
        )
        await s.flush()
        statuses = await luciel_api._tool_statuses(s, admin, iid)
        out = luciel_api._to_out(inst, tool_status=statuses)
    by_id = {t.id: t for t in out.tools}
    assert by_id["send_sms"].connection_status == "connected"
    assert by_id["send_email"].connection_status == "expired"
    # A tool with no connection requirement is left alone.
    assert by_id["schedule_callback"].connection_status is None
    assert statuses["push_to_crm"] == "unconfigured"  # absent connection → unconfigured


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_callback_over_a_channel_that_cannot_deliver_is_refused_before_any_write():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    tool = ScheduleCallbackTool()
    async with tenant_session(admin) as s:
        ctx = ToolContext(session=s, admin_id=admin, instance_id=iid)
        for channel in ("widget", "voice"):
            res = await tool.execute(
                {"fire_at": "2030-01-01T10:00:00Z", "channel": channel, "message": "hi"}, ctx
            )
            assert res.outcome is Outcome.failed, channel
            assert res.reason == f"unsupported_channel:{channel}"
    assert c is not None
