"""Config-snapshot: additive/neutral pillar changes defer to the NEXT conversation
(Architecture §3.7.1 / §3.8.7 rule E / Decision #39).

A conversation freezes its EFFECTIVE additive/neutral config (enabled tools,
personality/business-context, enabled channels) when it BEGINS. Additive edits made
mid-conversation apply to NEW conversations only — they must not shift an in-flight
one. The SPLIT proven here:
  * CAPABILITY = snapshot: the set of enabled tools/channels/context is the frozen
    open-time view; a tool enabled AFTER open is unavailable to that session.
  * USABILITY = live gate: connection health + disable/revoke stay LIVE in the broker,
    so a mid-conversation disable/revoke still blocks immediately (safety wins) even
    for a capability that WAS in the snapshot.
A NULL snapshot (older session) falls back to the live instance config.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.db.session import tenant_session
from app.models.conversation import Session as SessionModel
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.tool_auth import InstanceToolAuthorization
from app.runtime.knowledge_retrieval import make_retriever
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import Orchestrator
from app.runtime.sessions import effective_config, resolve_or_open_session
from app.runtime.testing import EchoModel
from app.tools import contract as c
from app.tools.contract import ToolContext, ToolResult
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

TOOL_ID = "book_appointment"


class _FakeTool:
    """A trivial no-connection, no-channel tool that always succeeds when executed.
    Used to prove the orchestrator's snapshot capability gate vs the broker's live
    authorization gate independently."""

    tool_id = TOOL_ID
    display_name = "Book"
    description = "book"
    requires_connection: str | None = None
    requires_channels: frozenset[str] = frozenset()
    execution_mode = "in_process"
    required_inputs: frozenset[str] = frozenset()

    async def execute(self, inputs: dict[str, Any], context: ToolContext) -> ToolResult:
        return c.ok({"booked": True})


async def _seed(
    admin_id: uuid.UUID,
    *,
    tools: list[dict[str, object]] | None = None,
    channels: list[dict[str, object]] | None = None,
    business: str = "",
) -> uuid.UUID:
    async with tenant_session(admin_id) as s:
        s.add(
            Account(
                id=admin_id,
                admin_id=admin_id,
                user_id=admin_id,
                email=f"{admin_id}@x.com",
                email_lower=f"{admin_id}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        iid = uuid.uuid4()
        s.add(
            Instance(
                id=iid,
                admin_id=admin_id,
                name="L",
                business_one_liner=business,
                state=LucielState.active,
                channels=channels or [],
                tools=tools or [],
                escalation={},
                personality={},
            )
        )
        await s.flush()
        return iid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_snapshot_captured_at_session_open():
    admin = uuid.uuid4()
    iid = await _seed(
        admin,
        tools=[{"id": TOOL_ID, "enabled": True}, {"id": "send_email", "enabled": False}],
        channels=[{"id": "widget", "enabled": True}, {"id": "sms", "enabled": False}],
        business="Acme sells widgets.",
    )
    async with tenant_session(admin) as s:
        sess, is_new = await resolve_or_open_session(
            s, admin_id=admin, instance_id=iid, channel="widget", resolved_lead_id=None
        )
        assert is_new
        snap = sess.config_snapshot
    assert snap is not None
    # Only the ENABLED tool/channel are captured; disabled ones are omitted.
    assert snap["enabled_tools"] == [TOOL_ID]
    assert snap["enabled_channels"] == ["widget"]
    assert snap["business_context"] == "Acme sells widgets."


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_additive_tool_deferred_for_in_flight_present_for_new_session():
    admin = uuid.uuid4()
    iid = await _seed(admin, tools=[], channels=[{"id": "widget", "enabled": True}])
    # Session A opens with NO tools enabled → its snapshot capability set is empty.
    async with tenant_session(admin) as s:
        sess_a, _ = await resolve_or_open_session(
            s, admin_id=admin, instance_id=iid, channel="widget", resolved_lead_id=None
        )
        snap_a = sess_a.config_snapshot
    # Admin enables a tool mid-flight (additive/neutral change).
    async with tenant_session(admin) as s:
        inst = (await s.execute(select(Instance).where(Instance.id == iid))).scalar_one()
        inst.tools = [{"id": TOOL_ID, "enabled": True}]
        await s.flush()
    # In-flight session A: the newly enabled tool is NOT a capability (deferred).
    async with tenant_session(admin) as s:
        inst = (await s.execute(select(Instance).where(Instance.id == iid))).scalar_one()
        eff_a = effective_config(snap_a, inst, always_include_channel="widget")
    assert eff_a.snapshot_tool_ids == set()  # empty at open → tool deferred
    # A NEW session B (widget always opens fresh — resolved_lead_id is None) sees it.
    async with tenant_session(admin) as s:
        sess_b, is_new_b = await resolve_or_open_session(
            s, admin_id=admin, instance_id=iid, channel="widget", resolved_lead_id=None
        )
        assert is_new_b
        inst = (await s.execute(select(Instance).where(Instance.id == iid))).scalar_one()
        eff_b = effective_config(sess_b.config_snapshot, inst, always_include_channel="widget")
    assert eff_b.snapshot_tool_ids == {TOOL_ID}  # present for the next conversation


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_personality_business_context_change_deferred():
    admin = uuid.uuid4()
    iid = await _seed(admin, business="OLD context")
    async with tenant_session(admin) as s:
        sess_a, _ = await resolve_or_open_session(
            s, admin_id=admin, instance_id=iid, channel="widget", resolved_lead_id=None
        )
        snap_a = sess_a.config_snapshot
    async with tenant_session(admin) as s:
        inst = (await s.execute(select(Instance).where(Instance.id == iid))).scalar_one()
        inst.business_one_liner = "NEW context"
        await s.flush()
    async with tenant_session(admin) as s:
        inst = (await s.execute(select(Instance).where(Instance.id == iid))).scalar_one()
        eff_a = effective_config(snap_a, inst)
        # In-flight session keeps the open-time context (deferred).
        assert eff_a.business_context == "OLD context"
        # A new session picks up the live context.
        sess_b, _ = await resolve_or_open_session(
            s, admin_id=admin, instance_id=iid, channel="widget", resolved_lead_id=None
        )
        eff_b = effective_config(sess_b.config_snapshot, inst)
    assert eff_b.business_context == "NEW context"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_null_snapshot_falls_back_to_live_config():
    admin = uuid.uuid4()
    iid = await _seed(
        admin,
        tools=[{"id": "send_email", "enabled": True}],
        channels=[{"id": "widget", "enabled": True}],
        business="LIVE",
    )
    # An older session predating the column has NULL config_snapshot.
    async with tenant_session(admin) as s:
        inst = (await s.execute(select(Instance).where(Instance.id == iid))).scalar_one()
        eff = effective_config(None, inst, always_include_channel="widget")
    # No snapshot filter (None) → rely on the broker's live gates; live config supplies
    # business_context + enabled channels.
    assert eff.snapshot_tool_ids is None
    assert eff.business_context == "LIVE"
    assert eff.enabled_channels == {"widget"}


async def _run_tool_turn(
    admin: uuid.UUID, iid: uuid.UUID, snapshot_tool_ids: set[str] | None, resolved: list[str]
) -> list[str]:
    async with tenant_session(admin) as s:
        sid = uuid.uuid4()
        s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel="widget", mode="ai"))
        await s.flush()

        def _resolver(tool_id: str):
            resolved.append(tool_id)
            return _FakeTool()

        orch = Orchestrator(
            session=s,
            router=LLMRouter(
                primary=EchoModel(confidence=0.9, tool_calls=[{"tool_id": TOOL_ID, "inputs": {}}]),
                fallback=EchoModel(),
            ),
            retriever=make_retriever(s, admin_id=admin, instance_id=iid),
            admin_id=admin,
            instance_id=iid,
            enabled_channels={"widget"},
            tool_resolver=_resolver,
            snapshot_tool_ids=snapshot_tool_ids,
        )
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="book me an appointment",
        )
        return result.tool_outcomes


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_capability_snapshot_defers_tool_not_in_set():
    """A tool NOT in the frozen snapshot capability set is unavailable to this
    conversation: the resolver is never even consulted, outcome is `failed`."""
    admin = uuid.uuid4()
    iid = await _seed(admin, channels=[{"id": "widget", "enabled": True}])
    # Authorize the tool LIVE — proving the snapshot (not the live gate) is what defers.
    async with tenant_session(admin) as s:
        s.add(
            InstanceToolAuthorization(
                admin_id=admin, instance_id=iid, tool_id=TOOL_ID, enabled=True
            )
        )
        await s.flush()
    resolved: list[str] = []
    outcomes = await _run_tool_turn(admin, iid, snapshot_tool_ids=set(), resolved=resolved)
    assert resolved == []  # capability gate short-circuits before resolution
    assert outcomes and all(o == "failed" for o in outcomes)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_capability_in_snapshot_and_live_authorized_succeeds():
    """A tool in the snapshot capability set AND live-authorized dispatches ok."""
    admin = uuid.uuid4()
    iid = await _seed(admin, channels=[{"id": "widget", "enabled": True}])
    async with tenant_session(admin) as s:
        s.add(
            InstanceToolAuthorization(
                admin_id=admin, instance_id=iid, tool_id=TOOL_ID, enabled=True
            )
        )
        await s.flush()
    resolved: list[str] = []
    outcomes = await _run_tool_turn(admin, iid, snapshot_tool_ids={TOOL_ID}, resolved=resolved)
    assert resolved == [TOOL_ID]  # capability present → resolver consulted
    assert outcomes == ["ok"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_live_disable_still_blocks_a_snapshotted_capability_immediately():
    """SAFETY half (usability=live): a tool that WAS in the snapshot capability set is
    still blocked immediately when live authorization is absent/revoked — the snapshot
    never resurrects a disabled/revoked dependency (§3.8.7 rule E safety side)."""
    admin = uuid.uuid4()
    iid = await _seed(admin, channels=[{"id": "widget", "enabled": True}])
    # NO InstanceToolAuthorization row → live authorization denied (mid-session disable).
    resolved: list[str] = []
    outcomes = await _run_tool_turn(admin, iid, snapshot_tool_ids={TOOL_ID}, resolved=resolved)
    # Capability present → the broker IS consulted (the loop retries the denied tool up
    # to MAX_ITERATIONS), but the LIVE authorization gate denies every attempt → failed.
    assert resolved and all(t == TOOL_ID for t in resolved)
    assert outcomes and all(o == "failed" for o in outcomes)
