"""Required-input resolution from the confirmed record (round 7 WP-2 remainder, item 10).

``ToolContext.gathered`` was declared and nothing wrote or read it: the broker judged
``required_inputs`` against the model's inputs alone, so an SMS lead who had already
confirmed their email was asked for it again the moment the model left it out of
``book_appointment``. Now the loop gathers the lead's CONFIRMED email/phone (the same
bar as sending to an address: transport-carried or confirmed in conversation), the
stanza names the fields, and the broker fills a missing required input from them —
an explicit input always wins, an unconfirmed address never fills anything.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.db.session import tenant_session
from app.models.conversation import Session as SessionModel
from app.models.instance import Instance, LucielState
from app.models.lead import Lead, LeadIdentifier
from app.models.tool_auth import InstanceToolAuthorization
from app.runtime import channel_switch
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import Orchestrator
from app.runtime.testing import EchoModel
from app.tools import contract as c
from app.tools.broker import ToolBroker
from app.tools.contract import ToolContext, ToolResult
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

PHONE = "+15551234567"
EMAIL = "jane@example.com"
SLOT = "2026-10-01T14:00:00+00:00"


class _RecordingEcho(EchoModel):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.prompts: list[str] = []

    async def complete(self, system_prompt, messages, *, tools=None):  # type: ignore[override]
        self.prompts.append(system_prompt)
        return await super().complete(system_prompt, messages, tools=tools)


class _BookingTool:
    tool_id = "book_appointment"
    requires_connection: str | None = None
    requires_channels: frozenset[str] = frozenset()
    execution_mode = "in_process"
    display_name = "Book"
    description = "book"
    required_inputs: frozenset[str] = frozenset({"slot", "email"})

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.contexts: list[ToolContext] = []

    async def execute(self, inputs: dict[str, Any], context: ToolContext) -> ToolResult:
        self.calls.append(dict(inputs))
        self.contexts.append(context)
        return c.ok({"bookingRef": "bk-1", "start": inputs["slot"], "email": inputs["email"]})


async def _empty_retriever(_q: str) -> list[str]:
    return []


async def _seed(s, admin: uuid.UUID, iid: uuid.UUID, tool: _BookingTool) -> uuid.UUID:
    s.add(
        Instance(
            id=iid,
            admin_id=admin,
            name="L",
            state=LucielState.active,
            channels=[{"id": "sms", "enabled": True}],
            tools=[],
            escalation={},
            personality={},
        )
    )
    sid = uuid.uuid4()
    s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel="sms", mode="ai"))
    s.add(
        InstanceToolAuthorization(
            admin_id=admin, instance_id=iid, tool_id=tool.tool_id, enabled=True
        )
    )
    await s.flush()
    return sid


def _orch(s, admin, iid, *, primary, tool: _BookingTool) -> Orchestrator:
    return Orchestrator(
        session=s,
        router=LLMRouter(primary=primary, fallback=EchoModel()),
        retriever=_empty_retriever,
        admin_id=admin,
        instance_id=iid,
        enabled_channels={"sms"},
        tool_resolver=lambda tool_id: tool if tool_id == tool.tool_id else None,
        has_payment_method=False,
    )


async def _sms_turn(orch: Orchestrator, sid: uuid.UUID, text: str):
    return await orch.run_turn(
        session_id=sid,
        resolved_lead_id=None,
        channel="sms",
        mode="ai",
        inbound_text=text,
        transport_identifier=PHONE,
        received_at=datetime.now(UTC),
    )


def _booking_model() -> _RecordingEcho:
    """The model books with the slot alone — it trusts the record for the email."""
    return _RecordingEcho(
        confidence=1.0,
        tool_call_script=[[{"tool_id": "book_appointment", "inputs": {"slot": SLOT}}]],
        fixed_reply=f"Booked for {SLOT} — ref bk-1.",
    )


async def _lead(s, admin: uuid.UUID, iid: uuid.UUID) -> Lead:
    return (
        await s.execute(select(Lead).where(Lead.admin_id == admin, Lead.instance_id == iid))
    ).scalar_one()


async def _session_row(s, sid: uuid.UUID) -> SessionModel:
    return (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_sms_lead_with_a_confirmed_email_is_not_asked_for_it_again():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        tool = _BookingTool()
        sid = await _seed(s, admin, iid, tool)
        # Turn 1 captures the lead by their number (confirmed at capture, it carried
        # the message); their email was confirmed in conversation earlier.
        opener = EchoModel(confidence=1.0, fixed_reply="Happy to help — when suits you?")
        await _sms_turn(_orch(s, admin, iid, primary=opener, tool=tool), sid, "hi, can I book?")
        lead = await _lead(s, admin, iid)
        await channel_switch.confirm_identifier(
            s,
            admin_id=admin,
            instance_id=iid,
            lead_id=lead.id,
            kind="email",
            value=EMAIL,
            source="volunteered",
        )
        assert await channel_switch.confirmed_inputs_on_file(
            s, admin_id=admin, instance_id=iid, lead_id=lead.id, transport_identifier=PHONE
        ) == {"email": EMAIL, "phone": PHONE}

        model = _booking_model()
        result = await _sms_turn(
            _orch(s, admin, iid, primary=model, tool=tool), sid, "2pm on the 1st"
        )
        # The broker filled the email from the record: no missing_input, no ask.
        assert result.tool_outcomes == ["ok"]
        assert tool.calls == [{"slot": SLOT, "email": EMAIL}]
        assert tool.contexts[0].gathered == {"email": EMAIL, "phone": PHONE}
        assert (await _session_row(s, sid)).input_ask_attempts == 0
        assert "?" not in result.reply_text
        # The model was told which fields the record holds — names, not values.
        assert "On file for this customer, confirmed: email, phone." in model.prompts[0]
        assert EMAIL not in model.prompts[0]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_unconfirmed_email_never_fills_a_required_input():
    """The lead mentioned an address once (captured on the record, never confirmed):
    the broker still refuses with missing_input — the same bar as sending to it."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        tool = _BookingTool()
        sid = await _seed(s, admin, iid, tool)
        opener = EchoModel(confidence=1.0, fixed_reply="Happy to help — when suits you?")
        await _sms_turn(
            _orch(s, admin, iid, primary=opener, tool=tool),
            sid,
            f"hi, my email is {EMAIL}, can I book?",
        )
        lead = await _lead(s, admin, iid)
        rows = (
            (
                await s.execute(
                    select(LeadIdentifier).where(
                        LeadIdentifier.lead_id == lead.id, LeadIdentifier.kind == "email"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [r.confirmed_at for r in rows] == [None]  # on the record, not confirmed
        assert await channel_switch.confirmed_inputs_on_file(
            s, admin_id=admin, instance_id=iid, lead_id=lead.id, transport_identifier=PHONE
        ) == {"phone": PHONE}

        model = _booking_model()
        result = await _sms_turn(
            _orch(s, admin, iid, primary=model, tool=tool), sid, "2pm on the 1st"
        )
        assert result.tool_outcomes == ["missing_input"]
        assert tool.calls == []
        assert (await _session_row(s, sid)).input_ask_attempts == 1
        assert "On file for this customer, confirmed: phone." in model.prompts[0]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_broker_fills_only_what_is_missing_and_traces_the_field_names():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        tool = _BookingTool()
        await _seed(s, admin, iid, tool)
        broker = ToolBroker(s)
        explicit = await broker.dispatch(
            tool,
            {"slot": SLOT, "email": "typed@example.com"},
            admin_id=admin,
            instance_id=iid,
            gathered={"email": EMAIL},
        )
        assert explicit.outcome is c.Outcome.ok
        assert tool.calls[-1]["email"] == "typed@example.com"  # an explicit input wins
        assert broker.traces[-1]["filled_from_record"] is None

        filled = await broker.dispatch(
            tool,
            {"slot": SLOT, "email": ""},
            admin_id=admin,
            instance_id=iid,
            gathered={"email": EMAIL},
        )
        assert filled.outcome is c.Outcome.ok
        assert tool.calls[-1]["email"] == EMAIL  # a blank counts as missing
        assert broker.traces[-1]["filled_from_record"] == ["email"]

        # Nothing on file for the field: the honest missing_input, as before.
        refused = await broker.dispatch(
            tool, {"slot": SLOT}, admin_id=admin, instance_id=iid, gathered={"phone": PHONE}
        )
        assert refused.outcome is c.Outcome.missing_input
        assert refused.missing_fields == ["email"]
        assert broker.traces[-1]["filled_from_record"] is None
