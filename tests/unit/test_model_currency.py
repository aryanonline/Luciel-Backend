"""model_currency watchdog (§3.4.3) — detection, auto-swap, and the no-false-alarm rule.

The config bridge made a model-retirement swap deploy-free; this sweep is the
detection half and (owner decision 2026-08-17) the remediation half. Pinned here:

  - presence is judged by the provider's per-id RETRIEVE (``GET /v1/models/{id}``),
    NEVER by listing membership: the first live run proved ``claude-haiku-4-5``
    retrieves 200 while the listing carries only the dated
    ``claude-haiku-4-5-20251001`` — list-membership would false-alarm (or
    false-SWAP) every aliased pin;
  - the paginated listing (has_more/after_id) is fetched only to resolve a
    successor once a pin is genuinely 404;
  - a retired CHAT pin auto-swaps to the newest same-family successor: the
    platform-secret write, the service redeploy, and the "swapped X → Y" email
    all happen, and the run does NOT raise (the condition is remediated);
  - the guard rails: no cross-family guess, an env-var pin is never overridden,
    and the EMBEDDING model is never swapped (vectors are model-specific);
  - a provider we cannot REACH is a WARNING, never an alert;
  - mock mode is a no-op that constructs no transport and reads no secret.
"""

from __future__ import annotations

import logging

import pytest
from app.runtime import model_currency
from app.runtime.model_currency import (
    MODEL_CURRENCY_ALERT,
    MODEL_CURRENCY_SWAP,
    anthropic_models,
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
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> dict:
        return self._payload


class FakeTransport:
    """Retrieve + listing fakes per provider; records every GET.

    ``existing`` drives the per-id retrieves (200 iff the id is in the set) —
    the ground truth. The listing pages/payloads are served ONLY when the
    checker asks for a successor. An Exception for a provider makes every call
    against it raise (the unreachable case).
    """

    def __init__(
        self,
        *,
        existing: set[str],
        anthropic_pages: list[dict] | Exception | None = None,
        openai_listing: dict | Exception | None = None,
    ) -> None:
        self._existing = existing
        self._anthropic_pages = anthropic_pages
        self._openai_listing = openai_listing
        self._anthropic_page_calls = 0
        self.calls: list[tuple[str, dict | None]] = []

    def listing_calls(self, provider: str) -> list[tuple[str, dict | None]]:
        return [(u, p) for u, p in self.calls if provider in u and u.endswith("/v1/models")]

    async def get(self, url: str, *, params=None, headers=None):
        self.calls.append((url, params))
        provider_error = self._anthropic_pages if "anthropic" in url else self._openai_listing
        if isinstance(provider_error, Exception):
            raise provider_error
        if url.endswith("/v1/models"):  # the listing
            if "anthropic" in url:
                page = self._anthropic_pages[self._anthropic_page_calls]
                self._anthropic_page_calls += 1
                return _Resp(page)
            return _Resp(self._openai_listing)
        model_id = url.rsplit("/", 1)[1]  # the per-id retrieve
        return _Resp({"id": model_id}, status=200 if model_id in self._existing else 404)

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
    items = [{"id": i, "created": c} for i, c in ids_with_created]
    first, rest = items[:1], items[1:]
    return [
        {"data": first, "has_more": True, "first_id": first[0]["id"], "last_id": first[0]["id"]},
        {"data": rest, "has_more": False},
    ]


def _openai_listing(*ids_with_created: tuple[str, int]) -> dict:
    return {"object": "list", "data": [{"id": i, "created": c} for i, c in ids_with_created]}


_SETTINGS_KW = {"ANTHROPIC_API_KEY": "sk-ant-fake", "OPENAI_API_KEY": "sk-oai-fake"}
ALL_PINS = {
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "gpt-5.4",
    "text-embedding-3-small",
}


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


async def test_anthropic_listing_walks_pagination():
    transport = FakeTransport(
        existing=set(),
        anthropic_pages=_anthropic_pages(("claude-sonnet-4-6", 300), ("claude-haiku-4-5", 200)),
    )
    items = await anthropic_models(transport, api_key="sk-ant-fake")
    assert [i["id"] for i in items] == ["claude-sonnet-4-6", "claude-haiku-4-5"]
    listing_calls = transport.listing_calls("anthropic")
    assert len(listing_calls) == 2
    assert listing_calls[1][1]["after_id"] == "claude-sonnet-4-6"


# --- the check ----------------------------------------------------------------- #


async def test_presence_is_retrieve_based_and_listing_free_when_all_pins_resolve():
    """THE live finding (2026-08-17), pinned: an aliased pin that retrieves 200
    is PRESENT even though the listing would only show its dated form. All pins
    resolving means the listing is never even fetched."""
    transport = FakeTransport(
        existing=set(ALL_PINS),
        # A listing that would falsely convict claude-haiku-4-5 if consulted:
        anthropic_pages=_anthropic_pages(
            ("claude-sonnet-4-6", 300), ("claude-haiku-4-5-20251001", 200)
        ),
        openai_listing=_openai_listing(("gpt-5.4", 400)),
    )
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
    assert transport.listing_calls("anthropic") == []
    assert transport.listing_calls("openai") == []


async def test_retired_chat_pin_auto_swaps_writes_restarts_emails_no_raise(caplog):
    # sonnet-4-6 genuinely 404s; the listing (fetched for the successor) has 4-8.
    transport = FakeTransport(
        existing=set(ALL_PINS) - {"claude-sonnet-4-6"},
        anthropic_pages=_anthropic_pages(("claude-sonnet-4-8", 500), ("claude-haiku-4-5", 200)),
        openai_listing=_openai_listing(("gpt-5.4", 400)),
    )
    sender, writer, restarter = RecordingSender(), FakeWriter(), FakeRestarter()
    with caplog.at_level(logging.WARNING, logger="app.runtime.model_currency"):
        result = await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW),
            transport_factory=lambda: transport,
            email_sender=sender,
            secret_writer=writer,
            restarter=restarter,
        )
    assert writer.applied == [{"LLM_ANTHROPIC_PRIMARY_MODEL": "claude-sonnet-4-8"}]
    assert restarter.restarts == 1
    assert len(transport.listing_calls("anthropic")) == 2  # paginated successor lookup
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
    transport = FakeTransport(
        existing=set(ALL_PINS) - {"claude-sonnet-4-6"},
        # Sonnet vanished entirely; only other families remain.
        anthropic_pages=_anthropic_pages(("claude-haiku-4-5", 200), ("claude-opus-5-1", 900)),
        openai_listing=_openai_listing(("gpt-5.4", 400)),
    )
    sender, writer = RecordingSender(), FakeWriter()
    with caplog.at_level(logging.ERROR, logger="app.runtime.model_currency"):
        with pytest.raises(RuntimeError, match="model_currency"):
            await run_model_currency_check(
                settings=live_settings(**_SETTINGS_KW),
                transport_factory=lambda: transport,
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
    transport = FakeTransport(
        existing=set(ALL_PINS) - {"claude-sonnet-4-6"},
        anthropic_pages=_anthropic_pages(("claude-sonnet-4-8", 500), ("claude-haiku-4-5", 200)),
        openai_listing=_openai_listing(("gpt-5.4", 400)),
    )
    sender, writer = RecordingSender(), FakeWriter()
    with pytest.raises(RuntimeError):
        await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW),
            transport_factory=lambda: transport,
            email_sender=sender,
            secret_writer=writer,
            restarter=FakeRestarter(),
        )
    assert writer.applied == []
    ((_to, _subject, body),) = sender.sent
    assert "env var" in body


async def test_embedding_model_is_alert_only_even_when_a_successor_exists():
    transport = FakeTransport(
        existing=set(ALL_PINS) - {"text-embedding-3-small"},
        openai_listing=_openai_listing(("gpt-5.4", 400), ("text-embedding-4-small", 900)),
    )
    sender, writer = RecordingSender(), FakeWriter()
    with pytest.raises(RuntimeError):
        await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW),
            transport_factory=lambda: transport,
            email_sender=sender,
            secret_writer=writer,
            restarter=FakeRestarter(),
        )
    assert writer.applied == []  # vectors are model-specific: NEVER swapped
    assert transport.listing_calls("openai") == []  # alert-only pins skip the listing
    ((_to, _subject, body),) = sender.sent
    assert "re-embedding" in body


async def test_secret_write_failure_becomes_an_alert_not_a_crash():
    transport = FakeTransport(
        existing=set(ALL_PINS) - {"claude-sonnet-4-6"},
        anthropic_pages=_anthropic_pages(("claude-sonnet-4-8", 500), ("claude-haiku-4-5", 200)),
        openai_listing=_openai_listing(("gpt-5.4", 400)),
    )
    sender = RecordingSender()
    with pytest.raises(RuntimeError, match="model_currency"):
        await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW),
            transport_factory=lambda: transport,
            email_sender=sender,
            secret_writer=FakeWriter(fail=True),
            restarter=FakeRestarter(),
        )
    ((_to, _subject, body),) = sender.sent
    assert "write failed" in body


async def test_unreachable_provider_warns_and_never_false_alarms(caplog):
    transport = FakeTransport(
        existing={"gpt-5.4", "text-embedding-3-small"},
        anthropic_pages=ConnectionError("anthropic down"),
        openai_listing=_openai_listing(("gpt-5.4", 400)),
    )
    sender, writer = RecordingSender(), FakeWriter()
    with caplog.at_level(logging.WARNING, logger="app.runtime.model_currency"):
        result = await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW),
            transport_factory=lambda: transport,
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
