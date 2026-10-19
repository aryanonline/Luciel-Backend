"""Round 7 WP-6 item 4: a dual-provider outage is an operator metric, not just a row.

The orchestrator's llm_unavailable path wrote an escalation row and gave the customer
the outage line, but emitted nothing an alarm could read: LLMFallbackUsed never fires
(the fallback never answered), so both providers could be down for an hour with no
page. It now emits ``LLMUnavailable`` (infra: LlmUnavailableAlarm). The tenant-cap
refusal keeps its own ``LLMCapExceeded`` and is NOT an outage.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.conversation import Session as SessionModel
from app.runtime import llm_cap as llm_cap_module
from app.runtime import orchestrator as orchestrator_module
from app.runtime.escalation import Signal
from app.runtime.llm_cap import LLM_CAP_PER_MINUTE, InMemoryLLMCapStore
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import Orchestrator
from app.runtime.testing import EchoModel

pytestmark = [pytest.mark.asyncio]


async def _no_chunks(_q: str) -> list[str]:
    return []


async def _seed_session(s, admin, iid, *, channel: str) -> uuid.UUID:
    sid = uuid.uuid4()
    s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel=channel, mode="ai"))
    await s.flush()
    return sid


def _record_metrics(monkeypatch) -> list[tuple[str, dict[str, str]]]:
    seen: list[tuple[str, dict[str, str]]] = []

    def fake_emit(name: str, value: float = 1.0, **kw) -> None:
        seen.append((name, dict(kw.get("dimensions") or {})))

    monkeypatch.setattr(orchestrator_module, "emit_metric", fake_emit)
    return seen


@pytest.mark.parametrize("channel", ["widget", "sms"])
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_both_providers_down_emits_llm_unavailable(monkeypatch, channel: str):
    seen = _record_metrics(monkeypatch)
    admin, iid = uuid.uuid4(), uuid.uuid4()
    down = EchoModel(raise_provider_error=True)
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid, channel=channel)
        orch = Orchestrator(
            session=s,
            router=LLMRouter(primary=down, fallback=down),
            retriever=_no_chunks,
            admin_id=admin,
            instance_id=iid,
            enabled_channels={"widget", "sms"},
            has_payment_method=False,
        )
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel=channel,
            mode="ai",
            inbound_text="are you open on saturday?",
            transport_identifier=None if channel == "widget" else "+16045550199",
        )
    assert result.escalation is not None
    assert result.escalation.signal is Signal.llm_unavailable
    assert ("LLMUnavailable", {"Channel": channel}) in seen
    assert [name for name, _ in seen].count("LLMUnavailable") == 1
    assert "LLMCapExceeded" not in [name for name, _ in seen]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_cap_refusal_is_not_an_outage(monkeypatch):
    seen = _record_metrics(monkeypatch)
    store = InMemoryLLMCapStore()
    monkeypatch.setattr(llm_cap_module, "_store", store)
    admin, iid = uuid.uuid4(), uuid.uuid4()
    for _ in range(LLM_CAP_PER_MINUTE):
        assert store.admit(str(admin)) is True
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid, channel="widget")
        orch = Orchestrator(
            session=s,
            router=LLMRouter(primary=EchoModel(), fallback=EchoModel(), admin_id=admin),
            retriever=_no_chunks,
            admin_id=admin,
            instance_id=iid,
            enabled_channels={"widget"},
            has_payment_method=False,
        )
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="what are your hours?",
        )
    assert result.escalation is not None
    assert result.escalation.signal is Signal.llm_unavailable
    names = [name for name, _ in seen]
    assert "LLMCapExceeded" in names and "LLMUnavailable" not in names
