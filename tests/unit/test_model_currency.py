"""model_currency watchdog (§3.4.3) — detection, auto-swap, and the no-false-alarm rule.

The config bridge made a model-retirement swap deploy-free; this sweep is the
detection half and (owner decision 2026-08-17) the remediation half. Pinned here:

  - the Anthropic models list is parsed PAGINATED (has_more/after_id) — the
    envelope contract our checker depends on;
  - a retired CHAT pin auto-swaps to the newest same-family successor: the
    platform-secret write, the service redeploy, and the "swapped X → Y" email
    all happen, and the run does NOT raise (the condition is remediated);
  - the guard rails: no cross-family guess, an env-var pin is never overridden,
    and the EMBEDDING model is never swapped (vectors are model-specific);
  - a provider we cannot REACH is a WARNING, never an alert — an unreachable
    models endpoint is not a retired model;
  - mock mode is a no-op that constructs no transport and reads no secret.
"""

from __future__ import annotations

import logging

import pytest
from app.runtime import model_currency
from app.runtime.model_currency import (
    MODEL_CURRENCY_ALERT,
    MODEL_CURRENCY_SWAP,
    model_family,
    pick_successor,
    run_model_currency_check,
)

from tests.unit.live_settings import live_settings

pytestmark = [pytest.mark.asyncio]


# --- fakes --------------------------------------------------------------------- #


class _Resp:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise RuntimeError(f"http {self._status}")

    def json(self) -> dict:
        return self._payload


class FakeTransport:
    """Queued responses per URL prefix; records every GET."""

    def __init__(self, anthropic: list[dict] | Exception, openai: dict | Exception) -> None:
        self._anthropic_pages = anthropic if isinstance(anthropic, list) else None
        self._anthropic_error = anthropic if isinstance(anthropic, Exception) else None
        self._openai = openai
        self._anthropic_calls = 0
        self.calls: list[tuple[str, dict | None]] = []

    async def get(self, url: str, *, params=None, headers=None):
        self.calls.append((url, params))
        if "anthropic" in url:
            if self._anthropic_error is not None:
                raise self._anthropic_error
            page = self._anthropic_pages[self._anthropic_calls]
            self._anthropic_calls += 1
            return _Resp(page)
        if isinstance(self._openai, Exception):
            raise self._openai
        return _Resp(self._openai)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


class ExplodingFactory:
    """A transport factory that must never be constructed (mock no-op proof)."""

    def __call__(self):
        raise AssertionError("mock mode must not construct a transport")


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, *, to, subject, body_text, from_address=None):
        self.sent.append((to, subject, body_text))
        return "msg-1"


class FakeWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.applied: list[dict[str, str]] = []
        self._fail = fail

    async def apply(self, updates: dict[str, str]) -> None:
        if self._fail:
            raise ConnectionError("secretsmanager down")
        self.applied.append(dict(updates))


class FakeRestarter:
    def __init__(self) -> None:
        self.restarts = 0

    async def restart(self) -> None:
        self.restarts += 1


def _anthropic_pages(*ids_with_created: tuple[str, int]) -> list[dict]:
    """Two pages, split after the first item — pins the has_more/after_id walk."""
    items = [{"id": i, "created_at": None, "created": c} for i, c in ids_with_created]
    first, rest = items[:1], items[1:]
    return [
        {"data": first, "has_more": True, "first_id": first[0]["id"], "last_id": first[0]["id"]},
        {"data": rest, "has_more": False},
    ]


def _openai_listing(*ids_with_created: tuple[str, int]) -> dict:
    return {"object": "list", "data": [{"id": i, "created": c} for i, c in ids_with_created]}


_SETTINGS_KW = {"ANTHROPIC_API_KEY": "sk-ant-fake", "OPENAI_API_KEY": "sk-oai-fake"}

ALL_CURRENT_ANTHROPIC = _anthropic_pages(("claude-sonnet-4-6", 300), ("claude-haiku-4-5", 200))
ALL_CURRENT_OPENAI = _openai_listing(("gpt-5.4", 400), ("text-embedding-3-small", 100))


@pytest.fixture(autouse=True)
def _no_env_pins(monkeypatch):
    for pin in model_currency.PINS:
        monkeypatch.delenv(pin.env_var, raising=False)
    monkeypatch.delenv("LUCIEL_PLATFORM_SECRET_ARN", raising=False)
    monkeypatch.delenv("LUCIEL_ECS_CLUSTER", raising=False)
    monkeypatch.delenv("LUCIEL_ECS_SERVICES", raising=False)


# --- successor resolution ------------------------------------------------------ #


def test_model_family_strips_versions_but_keeps_names():
    assert model_family("claude-sonnet-4-6") == "claude-sonnet"
    assert model_family("claude-haiku-4-5-20251001") == "claude-haiku"
    assert model_family("gpt-5.4") == "gpt-5"
    assert model_family("gpt-4o") == "gpt-4o"  # non-numeric segment is the name
    assert model_family("text-embedding-3-small") == "text-embedding-3-small"


def test_pick_successor_newest_same_family_never_cross_family():
    listing = [
        {"id": "claude-sonnet-4-8", "created": 500},
        {"id": "claude-sonnet-4-7", "created": 400},
        {"id": "claude-haiku-4-9", "created": 900},  # newer but the WRONG family
        {"id": "gpt-6", "created": 999},
    ]
    assert pick_successor("claude-sonnet-4-6", listing) == "claude-sonnet-4-8"
    # gpt-5.4's family is gpt-5; gpt-6 is not a same-family successor.
    assert pick_successor("gpt-5.4", [{"id": "gpt-6", "created": 999}]) is None
    assert pick_successor("gpt-5.4", [{"id": "gpt-5.6", "created": 999}]) == "gpt-5.6"


# --- the check ----------------------------------------------------------------- #


async def test_all_pins_present_parses_the_paginated_envelope_and_stays_quiet():
    transport = FakeTransport(ALL_CURRENT_ANTHROPIC, ALL_CURRENT_OPENAI)
    sender = RecordingSender()
    result = await run_model_currency_check(
        settings=live_settings(**_SETTINGS_KW),
        transport_factory=lambda: transport,
        email_sender=sender,
        secret_writer=FakeWriter(),
        restarter=FakeRestarter(),
    )
    assert len(result["checked"]) == 4 and result["swapped"] == []
    assert sender.sent == []
    # The pagination walk actually happened: two Anthropic GETs, the second
    # carrying after_id from the first page's last item.
    anthropic_calls = [(u, p) for u, p in transport.calls if "anthropic" in u]
    assert len(anthropic_calls) == 2
    assert anthropic_calls[1][1]["after_id"] == "claude-sonnet-4-6"


async def test_retired_chat_pin_auto_swaps_writes_restarts_emails_no_raise(caplog):
    anthropic = _anthropic_pages(("claude-sonnet-4-8", 500), ("claude-haiku-4-5", 200))
    sender, writer, restarter = RecordingSender(), FakeWriter(), FakeRestarter()
    with caplog.at_level(logging.WARNING, logger="app.runtime.model_currency"):
        result = await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW),
            transport_factory=lambda: FakeTransport(anthropic, ALL_CURRENT_OPENAI),
            email_sender=sender,
            secret_writer=writer,
            restarter=restarter,
        )
    assert writer.applied == [{"LLM_ANTHROPIC_PRIMARY_MODEL": "claude-sonnet-4-8"}]
    assert restarter.restarts == 1
    ((to, subject, body),) = sender.sent
    assert "swapped" in subject and "claude-sonnet-4-6" in subject
    assert "claude-sonnet-4-8" in body and "No action is required" in body
    assert to == "no-reply@unit.test"  # the operator inbox (platform_email_sender)
    assert any(MODEL_CURRENCY_SWAP in r.getMessage() for r in caplog.records)
    assert result["swapped"] == [
        {
            "setting": "llm_anthropic_primary_model",
            "old": "claude-sonnet-4-6",
            "new": "claude-sonnet-4-8",
        }
    ]


async def test_no_same_family_successor_alerts_and_raises(caplog):
    # Sonnet vanished entirely; only other families remain.
    anthropic = _anthropic_pages(("claude-haiku-4-5", 200), ("claude-opus-5-1", 900))
    sender, writer = RecordingSender(), FakeWriter()
    with caplog.at_level(logging.ERROR, logger="app.runtime.model_currency"):
        with pytest.raises(RuntimeError, match="model_currency"):
            await run_model_currency_check(
                settings=live_settings(**_SETTINGS_KW),
                transport_factory=lambda: FakeTransport(anthropic, ALL_CURRENT_OPENAI),
                email_sender=sender,
                secret_writer=writer,
                restarter=FakeRestarter(),
            )
    assert writer.applied == []  # never a cross-family guess
    ((_to, subject, body),) = sender.sent
    assert "alert" in subject.lower() and "no same-family successor" in body
    assert any(MODEL_CURRENCY_ALERT in r.getMessage() for r in caplog.records)


async def test_env_pinned_model_is_never_overridden(monkeypatch):
    monkeypatch.setenv("LUCIEL_LLM_ANTHROPIC_PRIMARY_MODEL", "claude-sonnet-4-6")
    anthropic = _anthropic_pages(("claude-sonnet-4-8", 500), ("claude-haiku-4-5", 200))
    sender, writer = RecordingSender(), FakeWriter()
    with pytest.raises(RuntimeError):
        await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW),
            transport_factory=lambda: FakeTransport(anthropic, ALL_CURRENT_OPENAI),
            email_sender=sender,
            secret_writer=writer,
            restarter=FakeRestarter(),
        )
    assert writer.applied == []
    ((_to, _subject, body),) = sender.sent
    assert "env var" in body


async def test_embedding_model_is_alert_only_even_when_a_successor_exists():
    openai = _openai_listing(("gpt-5.4", 400), ("text-embedding-4-small", 900))
    sender, writer = RecordingSender(), FakeWriter()
    with pytest.raises(RuntimeError):
        await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW),
            transport_factory=lambda: FakeTransport(ALL_CURRENT_ANTHROPIC, openai),
            email_sender=sender,
            secret_writer=writer,
            restarter=FakeRestarter(),
        )
    assert writer.applied == []  # vectors are model-specific: NEVER swapped
    ((_to, _subject, body),) = sender.sent
    assert "re-embedding" in body


async def test_secret_write_failure_becomes_an_alert_not_a_crash():
    anthropic = _anthropic_pages(("claude-sonnet-4-8", 500), ("claude-haiku-4-5", 200))
    sender = RecordingSender()
    with pytest.raises(RuntimeError, match="model_currency"):
        await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW),
            transport_factory=lambda: FakeTransport(anthropic, ALL_CURRENT_OPENAI),
            email_sender=sender,
            secret_writer=FakeWriter(fail=True),
            restarter=FakeRestarter(),
        )
    ((_to, _subject, body),) = sender.sent
    assert "write failed" in body


async def test_unreachable_provider_warns_and_never_false_alarms(caplog):
    sender, writer = RecordingSender(), FakeWriter()
    with caplog.at_level(logging.WARNING, logger="app.runtime.model_currency"):
        result = await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW),
            transport_factory=lambda: FakeTransport(
                ConnectionError("anthropic down"), ALL_CURRENT_OPENAI
            ),
            email_sender=sender,
            secret_writer=writer,
            restarter=FakeRestarter(),
        )
    # No verdict on the unreachable provider's pins; the other provider is still
    # checked; nothing raised, nothing emailed, nothing swapped.
    assert result["unreachable"] == ["anthropic"]
    assert sorted(result["checked"]) == [
        "knowledge_embedding_model",
        "llm_openai_fallback_model",
    ]
    assert sender.sent == [] and writer.applied == []
    assert any("model_currency_provider_unreachable" in r.getMessage() for r in caplog.records)
    assert not any(MODEL_CURRENCY_ALERT in r.getMessage() for r in caplog.records)


async def test_mock_mode_is_a_no_op_that_constructs_nothing():
    result = await run_model_currency_check(transport_factory=ExplodingFactory())
    assert result == {"skipped": "mock"}


async def test_missing_provider_keys_is_a_loud_misconfiguration(caplog):
    with caplog.at_level(logging.ERROR, logger="app.runtime.model_currency"):
        with pytest.raises(RuntimeError, match="missing provider keys"):
            await run_model_currency_check(
                settings=live_settings(ANTHROPIC_API_KEY="sk-ant-fake"),  # no OpenAI key
                transport_factory=ExplodingFactory(),
            )
    assert any(MODEL_CURRENCY_ALERT in r.getMessage() for r in caplog.records)
