"""Round 7 WP-6 item 4: a dual-provider outage is an operator metric, not just a row.

The orchestrator's llm_unavailable path wrote an escalation row and gave the customer
the outage line, but emitted nothing an alarm could read: LLMFallbackUsed never fires
(the fallback never answered), so both providers could be down for an hour with no
page. It now emits ``LLMUnavailable`` (infra: LlmUnavailableAlarm). The tenant-cap
refusal keeps its own ``LLMCapExceeded`` and is NOT an outage.

A turn either alarm counts also carries the router's cause lines (``llm_failover`` for
the primary, ``llm_fallback_failed`` when the fallback fails too): which provider failed
and how, never what the customer said.
"""

from __future__ import annotations

import logging
import re
import uuid

import pytest
from app.db.session import tenant_session
from app.models.conversation import Session as SessionModel
from app.observability.logging import StructuredFormatter
from app.runtime import llm_cap as llm_cap_module
from app.runtime import orchestrator as orchestrator_module
from app.runtime.escalation import Signal
from app.runtime.llm_cap import LLM_CAP_PER_MINUTE, InMemoryLLMCapStore
from app.runtime.llm_router import LLMRouter, ProviderError, admin_id_hash
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


class _Refusing(EchoModel):
    """A provider refusing every call with a ``ProviderError`` whose message echoes the
    whole thread it was sent (the customer's words among it), as an SDK message can echo
    the request. ``raised`` keeps each message, so a test can show the words were in it.
    The thread, not just its last message: the orchestrator may append a knowledge note
    after the customer's turn."""

    def __init__(self, *, name: str, status: int, kind: str, retryable: bool = False) -> None:
        super().__init__(name=name)
        self._status, self._kind, self._retryable = status, kind, retryable
        self.raised: list[str] = []

    async def complete(self, system_prompt, messages, *, tools=None):
        message = f"{self.name}_error: " + " | ".join(str(m.get("content")) for m in messages)
        self.raised.append(message)
        raise ProviderError(
            message, retryable=self._retryable, status_code=self._status, kind=self._kind
        )


def _cause_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.getMessage().startswith(("llm_failover ", "llm_fallback_failed "))
    ]


def _assert_never_logged(records: list[logging.LogRecord], *texts: str) -> None:
    """Not in the message, not on the record (args, extras, ``exc_info``), not in the
    JSON the formatter writes; and no traceback, which would print the message."""
    for record in records:
        assert record.exc_info is None
        for where in (
            record.getMessage(),
            repr(vars(record)),
            StructuredFormatter().format(record),
        ):
            for text in texts:
                assert text not in where


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_turn_the_fallback_answered_logs_why_the_primary_failed(monkeypatch, caplog):
    """LLMFallbackUsed counts the turn for LlmFallbackAlarm; the router's llm_failover
    line, written in the same turn, says why. Never the customer's words."""
    seen = _record_metrics(monkeypatch)
    admin, iid = uuid.uuid4(), uuid.uuid4()
    words = "are you open on saturday? this is Jane Roe"
    primary = _Refusing(name="anthropic", status=529, kind="OverloadedError", retryable=True)
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
    (record,) = _cause_records(caplog)
    assert re.fullmatch(
        r"llm_failover admin_id_hash=- leg=primary provider=anthropic model=- "
        r"error=OverloadedError status=529 retryable=True latency_ms=\d+",
        record.getMessage(),
    )
    # The customer's words were in what the primary raised: the check is not vacuous.
    assert len(primary.raised) == 1 and words in primary.raised[0]
    _assert_never_logged([record], words, "saturday", "Jane Roe")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_turn_no_model_served_logs_why_each_provider_failed(monkeypatch, caplog):
    """LLMUnavailable counts the turn for LlmUnavailableAlarm ("check provider status and
    the platform keys"); the router's two lines, written in the same turn, say which
    provider failed and why, here a revoked fallback key. Never the customer's words."""
    seen = _record_metrics(monkeypatch)
    monkeypatch.setattr(llm_cap_module, "_store", InMemoryLLMCapStore())
    admin, iid = uuid.uuid4(), uuid.uuid4()
    words = "are you open on saturday? this is Jane Roe"
    primary = _Refusing(name="anthropic", status=529, kind="OverloadedError", retryable=True)
    fallback = _Refusing(name="openai", status=401, kind="AuthenticationError")
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid, channel="widget")
        orch = Orchestrator(
            session=s,
            router=LLMRouter(primary=primary, fallback=fallback, admin_id=admin),
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
    assert result.escalation is not None
    assert result.escalation.signal is Signal.llm_unavailable
    assert [name for name, _ in seen].count("LLMUnavailable") == 1
    tenant = admin_id_hash(admin)
    records = _cause_records(caplog)
    assert [r.levelno for r in records] == [logging.WARNING, logging.WARNING]
    assert re.fullmatch(
        rf"llm_failover admin_id_hash={tenant} leg=primary provider=anthropic model=- "
        r"error=OverloadedError status=529 retryable=True latency_ms=\d+",
        records[0].getMessage(),
    )
    assert re.fullmatch(
        rf"llm_fallback_failed admin_id_hash={tenant} leg=fallback provider=openai model=- "
        r"error=AuthenticationError status=401 retryable=False latency_ms=\d+ attempt=1",
        records[1].getMessage(),
    )
    assert len(fallback.raised) == 1 and words in fallback.raised[0]
    _assert_never_logged(records, words, "saturday", "Jane Roe")
    # The lines name the tenant by hash; the raw id is the JSON formatter's (F035).
    assert all(str(admin) not in r.getMessage() for r in records)
