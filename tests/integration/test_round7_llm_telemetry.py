"""Round 7 WP-6 item 4: a dual-provider outage is an operator metric, not just a row.

The orchestrator's llm_unavailable path wrote an escalation row and gave the customer
the outage line, but emitted nothing an alarm could read: LLMFallbackUsed never fires
(the fallback never answered), so both providers could be down for an hour with no
page. It now emits ``LLMUnavailable`` (infra: LlmUnavailableAlarm). The tenant-cap
refusal keeps its own ``LLMCapExceeded`` and is NOT an outage.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from app.db.session import tenant_session
from app.models.conversation import Session as SessionModel
from app.runtime import llm_cap as llm_cap_module
from app.runtime import orchestrator as orchestrator_module
from app.runtime.escalation import Signal
from app.runtime.llm_cap import LLM_CAP_PER_MINUTE, InMemoryLLMCapStore
from app.runtime.llm_router import LLMRouter, ProviderError
from app.runtime.orchestrator import Orchestrator
from app.runtime.testing import EchoModel


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


class _OverloadedPrimary(EchoModel):
    """The Anthropic primary refusing with a 529, its message echoing the customer."""

    async def complete(self, system_prompt, messages, *, tools=None):
        echoed = messages[-1]["content"]
        raise ProviderError(
            f"anthropic_error: {echoed}", retryable=True, status_code=529, kind="OverloadedError"
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_turn_the_fallback_answered_logs_why_the_primary_failed(monkeypatch, caplog):
    """LLMFallbackUsed counts the turn for LlmFallbackAlarm; the router's llm_failover
    line, written in the same turn, says why. Never the customer's words."""
    seen = _record_metrics(monkeypatch)
    admin, iid = uuid.uuid4(), uuid.uuid4()
    words = "are you open on saturday? this is Jane Roe"
    primary = _OverloadedPrimary(name="anthropic")
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid, channel="widget")
        orch = Orchestrator(
            session=s,
            router=LLMRouter(primary=primary, fallback=EchoModel(name="openai")),
            retriever=_no_chunks,
            admin_id=admin,
            instance_id=iid,
            enabled_channels={"widget"},
            has_payment_method=False,
        )
        with caplog.at_level(logging.WARNING, logger="app.runtime.llm_router"):
            result = await orch.run_turn(
                session_id=sid,
                resolved_lead_id=None,
                channel="widget",
                mode="ai",
                inbound_text=words,
            )
    assert result.used_fallback is True
    assert [name for name, _ in seen].count("LLMFallbackUsed") == 1
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("llm_failover ")]
    assert len(lines) == 1
    assert (
        " leg=primary provider=anthropic model=- error=OverloadedError status=529 retryable=True "
        in lines[0]
    )
    assert "saturday" not in lines[0] and "Jane Roe" not in lines[0]
