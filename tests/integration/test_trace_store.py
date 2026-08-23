"""Durable trace store (§5.4 / §3.3.4 rule 3 — audit round 3, C6).

The broker's traces used to live in an in-memory list that died with the request;
"every outcome is traced" is only true if a row survives. Covers: one
tool_trace_events row per dispatch outcome (with session linkage, named missing
fields, non-secret reasons); retrieval_trace_events rows per (answer turn,
source) deduped across chunks of one document; and the 1-year retention prune
for both.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.db.session import tenant_session
from app.lifecycle.retention import expire_aged_data
from app.models.conversation import Session as SessionModel
from app.models.tool_auth import InstanceToolAuthorization
from app.models.trace import RetrievalTraceEvent, ToolTraceEvent
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import Orchestrator
from app.runtime.testing import EchoModel
from app.tools import contract as c
from app.tools.broker import ToolBroker
from app.tools.contract import ToolContext, ToolResult
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]


class _FakeTool:
    requires_connection: str | None = None
    requires_channels: frozenset[str] = frozenset()
    execution_mode = "in_process"
    display_name = "Fake"
    description = "fake"

    def __init__(
        self, tool_id: str, result: ToolResult, required_inputs: frozenset[str] = frozenset()
    ) -> None:
        self.tool_id = tool_id
        self._result = result
        self.required_inputs = required_inputs

    async def execute(self, inputs: dict[str, Any], context: ToolContext) -> ToolResult:
        return self._result


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_broker_persists_a_trace_row_per_outcome():
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            InstanceToolAuthorization(
                admin_id=admin, instance_id=iid, tool_id="lookup_record", enabled=True
            )
        )
        await s.flush()
        broker = ToolBroker(s)
        # ok
        await broker.dispatch(
            _FakeTool("lookup_record", c.ok({"record": {}})),
            {"query": "x"},
            admin_id=admin,
            instance_id=iid,
            session_id=sid,
        )
        # missing_input (broker backstop — named fields persisted)
        await broker.dispatch(
            _FakeTool("lookup_record", c.ok({}), required_inputs=frozenset({"query"})),
            {},
            admin_id=admin,
            instance_id=iid,
            session_id=sid,
        )
        # not_authorized failed (unauthorized tool id)
        await broker.dispatch(
            _FakeTool("send_email", c.ok({})),
            {},
            admin_id=admin,
            instance_id=iid,
            session_id=sid,
        )
        rows = (
            (
                await s.execute(
                    select(ToolTraceEvent)
                    .where(ToolTraceEvent.admin_id == admin)
                    .order_by(ToolTraceEvent.created_at)
                )
            )
            .scalars()
            .all()
        )
        assert [r.outcome for r in rows] == ["ok", "missing_input", "failed"]
        assert all(r.session_id == sid for r in rows)
        assert rows[1].missing_fields == ["query"]
        assert rows[2].reason == "not_authorized"


async def _empty_retriever(_q: str) -> list[str]:
    return []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_loop_persists_retrieval_rows_deduped_per_source():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    src_a, src_b = uuid.uuid4(), uuid.uuid4()

    async def _attributor(_q: str) -> list[dict[str, object]]:
        # Two chunks of source A + one of source B → exactly TWO rows.
        return [
            {"source_id": str(src_a), "source_name": "Brochure", "text": "t1"},
            {"source_id": str(src_a), "source_name": "Brochure", "text": "t2"},
            {"source_id": str(src_b), "source_name": "FAQ", "text": "t3"},
        ]

    async with tenant_session(admin) as s:
        sid = uuid.uuid4()
        s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel="widget", mode="ai"))
        await s.flush()
        orch = Orchestrator(
            session=s,
            router=LLMRouter(primary=EchoModel(confidence=0.9), fallback=EchoModel()),
            retriever=_empty_retriever,
            chunk_attributor=_attributor,
            admin_id=admin,
            instance_id=iid,
            enabled_channels={"widget"},
            has_payment_method=False,
        )
        await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="what services do you offer?",
        )
        rows = (
            (
                await s.execute(
                    select(RetrievalTraceEvent).where(RetrievalTraceEvent.admin_id == admin)
                )
            )
            .scalars()
            .all()
        )
        assert {r.source_id for r in rows} == {src_a, src_b}
        assert len(rows) == 2
        assert all(r.session_id == sid and r.transcript_id is not None for r in rows)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_retention_prunes_aged_trace_rows():
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    old = datetime.now(UTC) - timedelta(days=400)
    async with tenant_session(admin) as s:
        s.add(
            ToolTraceEvent(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                session_id=sid,
                tool_id="lookup_record",
                outcome="ok",
                created_at=old,
            )
        )
        s.add(
            RetrievalTraceEvent(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                session_id=sid,
                source_id=uuid.uuid4(),
                created_at=old,
            )
        )
        s.add(
            ToolTraceEvent(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                session_id=sid,
                tool_id="lookup_record",
                outcome="ok",
            )
        )
        await s.flush()
        counts = await expire_aged_data(s, admin_id=admin)
        assert counts.get("tool_trace_event") == 1
        assert counts.get("retrieval_trace_event") == 1
        remaining = (
            (await s.execute(select(ToolTraceEvent).where(ToolTraceEvent.admin_id == admin)))
            .scalars()
            .all()
        )
        assert len(remaining) == 1
