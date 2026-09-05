"""Live tool-use loop mechanics (§3.3.7 / §3.4.1 — audit round 3, C4).

Scripted EchoModel sequences drive the REAL loop + broker + contract code:
structured tool_result feedback (payloads, NAMED missing fields, candidate lists),
natural termination on a no-tool-calls response, the deterministic ask-attempt
bound (stalled gathers hand off instead of nagging), the faithful-readback
enforcement (unfounded success claims replaced; confirmed payloads appended on a
number mismatch), distinct snapshot/unavailable refusal reasons, and the
budget_exhausted decision/row signal harmony.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.db.session import tenant_session
from app.models.conversation import EscalationEvent
from app.models.conversation import Session as SessionModel
from app.models.tool_auth import InstanceToolAuthorization
from app.runtime import budget
from app.runtime.escalation import Signal
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import (
    _UNFOUNDED_CLAIM_REPLACEMENT,
    ASK_ATTEMPT_BOUND,
    Orchestrator,
)
from app.runtime.testing import EchoModel
from app.tools import contract as c
from app.tools.contract import ToolContext, ToolResult
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]


async def _empty_retriever(_q: str) -> list[str]:
    return []


class _RecordingEcho(EchoModel):
    """EchoModel that records every (system_prompt, messages, tools) it was given."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.turns: list[tuple[str, list[dict[str, Any]], Any]] = []

    async def complete(self, system_prompt, messages, *, tools=None):  # type: ignore[override]
        self.turns.append((system_prompt, [dict(m) for m in messages], tools))
        return await super().complete(system_prompt, messages, tools=tools)


class _FakeTool:
    """Minimal Tool with a scripted result (no connection, no channels)."""

    requires_connection: str | None = None
    requires_channels: frozenset[str] = frozenset()
    execution_mode = "in_process"
    display_name = "Fake"
    description = "fake"

    def __init__(
        self,
        tool_id: str,
        result: ToolResult,
        required_inputs: frozenset[str] = frozenset(),
    ) -> None:
        self.tool_id = tool_id
        self._result = result
        self.required_inputs = required_inputs
        self.calls: list[dict[str, Any]] = []

    async def execute(self, inputs: dict[str, Any], context: ToolContext) -> ToolResult:
        self.calls.append(dict(inputs))
        return self._result


async def _seed(s, admin, iid, *, tool: _FakeTool | None, channel: str = "widget") -> uuid.UUID:
    sid = uuid.uuid4()
    s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel=channel, mode="ai"))
    if tool is not None:
        s.add(
            InstanceToolAuthorization(
                admin_id=admin, instance_id=iid, tool_id=tool.tool_id, enabled=True
            )
        )
    await s.flush()
    return sid


def _orch(s, admin, iid, *, primary, tool: _FakeTool | None, snapshot=None) -> Orchestrator:
    def _resolver(tool_id: str):
        return tool if tool is not None and tool.tool_id == tool_id else None

    return Orchestrator(
        session=s,
        router=LLMRouter(primary=primary, fallback=EchoModel()),
        retriever=_empty_retriever,
        admin_id=admin,
        instance_id=iid,
        enabled_channels={"widget"},
        tool_resolver=_resolver,
        snapshot_tool_ids=snapshot,
        has_payment_method=False,
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_ok_call_feeds_payload_back_and_terminates_naturally():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        tool = _FakeTool("lookup_record", c.ok({"record": {"name": "Premium", "price": "$99"}}))
        sid = await _seed(s, admin, iid, tool=tool)
        primary = _RecordingEcho(
            tool_call_script=[[{"tool_id": "lookup_record", "inputs": {"query": "premium"}}]],
            fixed_reply="The Premium package is $99.",
        )
        result = await _orch(s, admin, iid, primary=primary, tool=tool).run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="how much is premium?",
        )
        # One proposing call + one natural-completion call = 2 iterations.
        assert result.iterations == 2
        assert result.tool_outcomes == ["ok"]
        assert tool.calls == [{"query": "premium"}]
        # The SECOND model call saw the assistant tool turn + a structured tool_result
        # carrying the actual payload — not a bare outcome-class string.
        _, second_messages, _ = primary.turns[1]
        roles = [m["role"] for m in second_messages]
        assert "tool_result" in roles
        feedback = next(m for m in second_messages if m["role"] == "tool_result")
        assert '"outcome": "ok"' in feedback["content"]
        assert "Premium" in feedback["content"]
        # The model was TOLD about the tool (defs) and the prompt carried the stanza.
        prompt, _, tools = primary.turns[0]
        assert tools is not None and tools[0]["tool_id"] == "lookup_record"
        assert "--- Tools" in prompt


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_missing_input_names_fields_and_counts_one_stall():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        tool = _FakeTool("book_appointment", c.ok({}), required_inputs=frozenset({"email"}))
        sid = await _seed(s, admin, iid, tool=tool)
        primary = _RecordingEcho(
            tool_call_script=[
                [{"tool_id": "book_appointment", "inputs": {"slot": "2026-09-01T14:00:00"}}]
            ],
            fixed_reply="What email should I send the confirmation to?",
        )
        result = await _orch(s, admin, iid, primary=primary, tool=tool).run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="book me for 2pm",
        )
        # Broker backstop fired BEFORE execution; the tool never ran (§3.3.7 rule 3).
        assert tool.calls == []
        assert result.tool_outcomes == ["missing_input"]
        # The feedback NAMED the missing field so the follow-up question is specific.
        _, second_messages, _ = primary.turns[1]
        feedback = next(m for m in second_messages if m["role"] == "tool_result")
        assert '"missing_fields": ["email"]' in feedback["content"]
        # Not an escalation — the model's ask goes out; the stall is counted.
        assert result.handed_off is False
        sess = (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()
        assert sess.input_ask_attempts == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_ask_bound_converts_repeated_stall_into_handoff():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        tool = _FakeTool("book_appointment", c.ok({}), required_inputs=frozenset({"email"}))
        sid = await _seed(s, admin, iid, tool=tool)

        def _turn_model() -> EchoModel:
            return EchoModel(
                tool_call_script=[
                    [{"tool_id": "book_appointment", "inputs": {"slot": "2026-09-01T14:00:00"}}]
                ],
                fixed_reply="Could I get your email?",
            )

        orch = _orch(s, admin, iid, primary=_turn_model(), tool=tool)
        first = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="book me for 2pm",
        )
        assert first.handed_off is False

        orch2 = _orch(s, admin, iid, primary=_turn_model(), tool=tool)
        second = await orch2.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="just book it",
        )
        # Second consecutive stall hits ASK_ATTEMPT_BOUND → honest handoff, not a nag
        # (§3.3.7 rule 4). Counter resets so a restored conversation starts clean.
        assert ASK_ATTEMPT_BOUND == 2
        assert second.handed_off is True
        assert second.escalation is not None
        assert second.escalation.signal is Signal.cannot_answer
        sess = (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()
        assert sess.mode == "human_controlled"
        assert sess.input_ask_attempts == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_ok_outcome_resets_the_stall_counter():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        stall_tool = _FakeTool("book_appointment", c.ok({}), required_inputs=frozenset({"email"}))
        sid = await _seed(s, admin, iid, tool=stall_tool)
        orch = _orch(
            s,
            admin,
            iid,
            primary=EchoModel(
                tool_call_script=[[{"tool_id": "book_appointment", "inputs": {"slot": "x"}}]],
                fixed_reply="What's your email?",
            ),
            tool=stall_tool,
        )
        await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="book me",
        )
        ok_tool = _FakeTool(
            "book_appointment",
            c.ok({"bookingRef": "abc", "start": "2026-09-01T14:00:00"}),
            required_inputs=frozenset({"email"}),
        )
        orch2 = _orch(
            s,
            admin,
            iid,
            primary=EchoModel(
                tool_call_script=[
                    [
                        {
                            "tool_id": "book_appointment",
                            "inputs": {"slot": "2026-09-01T14:00:00", "email": "j@x.com"},
                        }
                    ]
                ],
                fixed_reply="Booked for 2026-09-01T14:00:00 — ref abc.",
            ),
            tool=ok_tool,
        )
        result = await orch2.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="j@x.com",
        )
        assert result.tool_outcomes == ["ok"]
        sess = (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()
        assert sess.input_ask_attempts == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_unfounded_success_claim_is_replaced_and_escalated():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        failing = _FakeTool("book_appointment", c.failed("slot_conflict"))
        sid = await _seed(s, admin, iid, tool=failing)
        orch = _orch(
            s,
            admin,
            iid,
            primary=EchoModel(
                tool_call_script=[
                    [
                        {
                            "tool_id": "book_appointment",
                            "inputs": {"slot": "2026-09-01T14:00:00", "email": "j@x.com"},
                        }
                    ]
                ],
                fixed_reply="Great news — you're booked for 2pm tomorrow!",
                confidence=1.0,
            ),
            tool=failing,
        )
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="book me for 2pm",
        )
        # §3.3.4 rule 1: no success-claim without an ok payload — the claim is
        # replaced with the honest ack and a human is flagged.
        assert _UNFOUNDED_CLAIM_REPLACEMENT in result.reply_text
        assert "you're booked" not in result.reply_text.lower()
        assert result.escalation is not None
        ev = (
            (await s.execute(select(EscalationEvent).where(EscalationEvent.session_id == sid)))
            .scalars()
            .all()
        )
        assert any(e.signal == "cannot_answer" for e in ev)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_confirmed_payload_appends_exact_details_on_number_mismatch():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        tool = _FakeTool(
            "book_appointment",
            c.ok({"bookingRef": "ref77", "start": "2026-09-01T14:00:00+00:00"}),
        )
        sid = await _seed(s, admin, iid, tool=tool)
        orch = _orch(
            s,
            admin,
            iid,
            primary=EchoModel(
                tool_call_script=[
                    [
                        {
                            "tool_id": "book_appointment",
                            "inputs": {"slot": "2026-09-01T14:00:00", "email": "j@x.com"},
                        }
                    ]
                ],
                # "3pm" is a number the payload does not contain → readback mismatch.
                fixed_reply="Done — you're booked for 3pm!",
                confidence=1.0,
            ),
            tool=tool,
        )
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="book me",
        )
        assert "To confirm the exact details:" in result.reply_text
        assert "ref77" in result.reply_text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_snapshot_and_unavailable_refusals_carry_distinct_reasons():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        tool = _FakeTool("lookup_record", c.ok({"record": {}}))
        sid = await _seed(s, admin, iid, tool=tool)
        # Snapshot excludes the tool → tool_not_in_snapshot (never a bare "failed").
        primary = _RecordingEcho(
            tool_call_script=[[{"tool_id": "lookup_record", "inputs": {"query": "x"}}]]
        )
        await _orch(s, admin, iid, primary=primary, tool=tool, snapshot=set()).run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="find x",
        )
        _, msgs, _ = primary.turns[1]
        feedback = next(m for m in msgs if m["role"] == "tool_result")
        assert "tool_not_in_snapshot" in feedback["content"]

        # Unknown tool id → tool_unavailable.
        sid2 = await _seed(s, admin, iid, tool=None)
        primary2 = _RecordingEcho(tool_call_script=[[{"tool_id": "no_such_tool", "inputs": {}}]])
        await _orch(s, admin, iid, primary=primary2, tool=None).run_turn(
            session_id=sid2,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="hi",
        )
        _, msgs2, _ = primary2.turns[1]
        feedback2 = next(m for m in msgs2 if m["role"] == "tool_result")
        assert "tool_unavailable" in feedback2["content"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_at_cap_decision_signal_matches_the_persisted_row():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        state0 = await budget.check_budget(s, admin_id=admin, instance_id=iid)
        for _ in range(state0.free_allowance):
            await budget.count_session_once(
                s, admin_id=admin, instance_id=iid, session_id=uuid.uuid4()
            )
        sid = await _seed(s, admin, iid, tool=None)
        result = await _orch(s, admin, iid, primary=EchoModel(), tool=None).run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="hello",
        )
        assert result.at_cap is True
        assert result.escalation is not None
        # C4 harmony fix: the decision names the SAME signal as the row.
        assert result.escalation.signal is Signal.budget_exhausted
        ev = (
            (await s.execute(select(EscalationEvent).where(EscalationEvent.session_id == sid)))
            .scalars()
            .all()
        )
        assert any(e.signal == "budget_exhausted" for e in ev)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_at_cap_turns_escalate_once_per_session():
    """Cost review (2026-09-05 audit): every at-cap turn wrote a fresh budget_exhausted
    row and notified the owner again. One conversation is one business event — the
    visitor's twentieth message at the cap is not the twentieth escalation."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        state0 = await budget.check_budget(s, admin_id=admin, instance_id=iid)
        for _ in range(state0.free_allowance):
            await budget.count_session_once(
                s, admin_id=admin, instance_id=iid, session_id=uuid.uuid4()
            )
        sid = await _seed(s, admin, iid, tool=None)
        orch = _orch(s, admin, iid, primary=EchoModel(), tool=None)
        for text in ("hello", "anyone there?", "hello??"):
            result = await orch.run_turn(
                session_id=sid,
                resolved_lead_id=None,
                channel="widget",
                mode="ai",
                inbound_text=text,
            )
            assert result.at_cap is True
            assert result.escalation is not None  # every turn still REPORTS the state
        rows = (
            (await s.execute(select(EscalationEvent).where(EscalationEvent.session_id == sid)))
            .scalars()
            .all()
        )
        assert [r.signal for r in rows] == ["budget_exhausted"]
