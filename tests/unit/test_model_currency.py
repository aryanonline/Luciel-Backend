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

import json
import logging

import pytest
from app.runtime import model_currency
from app.runtime.model_currency import (
    MODEL_CURRENCY_ALERT,
    MODEL_CURRENCY_KEY_REJECTED,
    MODEL_CURRENCY_SWAP,
    anthropic_models,
    model_family,
    pick_newer_successor,
    pick_successor,
    run_model_currency_check,
)

from tests.unit.live_settings import live_settings

pytestmark = [pytest.mark.asyncio]


# --- fakes --------------------------------------------------------------------- #


class _HttpError(RuntimeError):
    """httpx.HTTPStatusError-shaped: carries the response the checker reads the
    status off (401/403 = key rejected, F133)."""

    def __init__(self, resp: _Resp) -> None:
        super().__init__(f"http {resp.status_code}")
        self.response = resp


class _Resp:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise _HttpError(self)

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
        smoke_fail: bool = False,
        key_rejected: frozenset[str] = frozenset(),
    ) -> None:
        self._existing = existing
        self._anthropic_pages = anthropic_pages
        self._openai_listing = openai_listing
        self._anthropic_page_calls = 0
        self._smoke_fail = smoke_fail
        self._key_rejected = key_rejected
        self.calls: list[tuple[str, dict | None]] = []
        self.posts: list[tuple[str, dict | None]] = []

    def listing_calls(self, provider: str) -> list[tuple[str, dict | None]]:
        return [(u, p) for u, p in self.calls if provider in u and u.endswith("/v1/models")]

    async def post(self, url: str, *, json=None, headers=None):
        """The smoke turn: one real completion against the candidate."""
        self.posts.append((url, json))
        if self._smoke_fail:
            return _Resp({"error": {"message": "model not available"}}, status=400)
        if "anthropic" in url:
            return _Resp({"content": [{"type": "text", "text": "OK"}]})
        return _Resp({"choices": [{"message": {"content": "OK"}}]})

    async def get(self, url: str, *, params=None, headers=None):
        self.calls.append((url, params))
        if any(provider in url for provider in self._key_rejected):
            return _Resp({}, status=401)  # the provider answered: the key is refused
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


async def test_weekly_nudge_when_a_strictly_newer_family_member_exists(caplog):
    """Phase 7 (owner concern #6): healthy pins + a newer same-family model in
    the listing → ONE weekly INFO email naming current and newer, nothing
    swapped, nothing raised — awareness, never an auto-upgrade."""
    from datetime import UTC, datetime

    transport = FakeTransport(
        existing=set(ALL_PINS),
        anthropic_pages=_anthropic_pages(("claude-sonnet-4-6", 300), ("claude-sonnet-5", 900)),
        openai_listing=_openai_listing(("gpt-5.4", 400)),
    )
    sender = RecordingSender()
    monday = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    assert monday.weekday() == 0
    with caplog.at_level(logging.INFO, logger="app.runtime.model_currency"):
        result = await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW),
            transport_factory=lambda: transport,
            email_sender=sender,
            secret_writer=FakeWriter(),
            restarter=FakeRestarter(),
            now=monday,
        )
    assert result["swapped"] == []
    assert result["nudges"] == [
        {
            "setting": "llm_anthropic_primary_model",
            "current": "claude-sonnet-4-6",
            "newer": "claude-sonnet-5",
        }
    ]
    ((to, subject, body),) = sender.sent
    assert "newer model" in subject
    assert "claude-sonnet-5" in body and "never upgraded automatically" in body
    assert to == "no-reply@unit.test"
    assert any(model_currency.MODEL_CURRENCY_NUDGE in r.getMessage() for r in caplog.records)


async def test_nudge_stays_quiet_off_day_and_when_the_pin_is_newest():
    from datetime import UTC, datetime

    # Same newer-model listing on a TUESDAY: no listing fetch, no email.
    transport = FakeTransport(
        existing=set(ALL_PINS),
        anthropic_pages=_anthropic_pages(("claude-sonnet-4-6", 300), ("claude-sonnet-5", 900)),
        openai_listing=_openai_listing(("gpt-5.4", 400)),
    )
    sender = RecordingSender()
    tuesday = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
    result = await run_model_currency_check(
        settings=live_settings(**_SETTINGS_KW),
        transport_factory=lambda: transport,
        email_sender=sender,
        now=tuesday,
    )
    assert result["nudges"] == [] and sender.sent == []
    assert transport.listing_calls("anthropic") == []

    # Monday, but the pin IS the family's newest (an older sibling remains
    # listed): pick_successor would offer the OLDER sibling — the strictness
    # rule keeps that non-news out of the inbox.
    transport = FakeTransport(
        existing=set(ALL_PINS),
        anthropic_pages=_anthropic_pages(("claude-sonnet-4-6", 900), ("claude-sonnet-4-5", 300)),
        openai_listing=_openai_listing(("gpt-5.4", 400)),
    )
    sender = RecordingSender()
    monday = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    result = await run_model_currency_check(
        settings=live_settings(**_SETTINGS_KW),
        transport_factory=lambda: transport,
        email_sender=sender,
        now=monday,
    )
    assert result["nudges"] == [] and sender.sent == []


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
    applied, marked = writer.applied
    assert applied["LLM_ANTHROPIC_PRIMARY_MODEL"] == "claude-sonnet-4-8"
    # The same rotation records the swap for the admins' 'model updated' notice (F114).
    record = json.loads(applied["LLM_MODEL_UPDATED"])
    assert record["from"] == "claude-sonnet-4-6" and record["to"] == "claude-sonnet-4-8"
    assert record["setting"] == "llm_anthropic_primary_model" and record["at"]
    # The restart is owed WITH the pin write and settled only once it succeeded
    # (round 7 WP-6 item 3).
    assert record["restart"] == "pending"
    assert json.loads(marked["LLM_MODEL_UPDATED"]) == {**record, "restart": "done"}
    assert set(marked) == {"LLM_MODEL_UPDATED"}
    # The successor answered a smoke turn BEFORE the secret was written (F113).
    ((smoke_url, smoke_payload),) = transport.posts
    assert "anthropic" in smoke_url and smoke_payload["model"] == "claude-sonnet-4-8"
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


# --- 2026-09-05 audit WP5-B: never older, smoke-validated, key-rejection is loud -- #


def test_pick_newer_successor_never_offers_an_older_sibling():
    # The retired pin WAS the family's newest: the only sibling is older. The old
    # rule would downgrade; the new one refuses (alert-only, a human decides).
    listing = [
        {"id": "claude-sonnet-4-7", "created": 400},
        {"id": "claude-haiku-4-9", "created": 900},
    ]
    assert pick_successor("claude-sonnet-4-8", listing) == "claude-sonnet-4-7"
    assert pick_newer_successor("claude-sonnet-4-8", listing) is None
    # A newer sibling wins; an older one in the same listing is ignored.
    assert (
        pick_newer_successor(
            "claude-sonnet-4-6",
            [
                {"id": "claude-sonnet-4-8", "created": 500},
                {"id": "claude-sonnet-4-5", "created": 100},
            ],
        )
        == "claude-sonnet-4-8"
    )
    # A dated snapshot of the SAME version is a legitimate successor for a retired alias.
    assert (
        pick_newer_successor(
            "claude-sonnet-4-6", [{"id": "claude-sonnet-4-6-20251101", "created": 500}]
        )
        == "claude-sonnet-4-6-20251101"
    )
    assert pick_newer_successor("gpt-5.4", [{"id": "gpt-5.2", "created": 999}]) is None
    assert pick_newer_successor("gpt-5.4", [{"id": "gpt-5.6", "created": 999}]) == "gpt-5.6"


async def test_a_retired_pin_with_only_older_siblings_alerts_instead_of_downgrading():
    transport = FakeTransport(
        existing=set(ALL_PINS) - {"claude-sonnet-4-6"},
        anthropic_pages=_anthropic_pages(("claude-sonnet-4-5", 100), ("claude-haiku-4-5", 200)),
        openai_listing=_openai_listing(("gpt-5.4", 400)),
    )
    sender, writer, restarter = RecordingSender(), FakeWriter(), FakeRestarter()
    with pytest.raises(RuntimeError):
        await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW),
            transport_factory=lambda: transport,
            email_sender=sender,
            secret_writer=writer,
            restarter=restarter,
        )
    assert writer.applied == [] and restarter.restarts == 0 and transport.posts == []
    ((_, subject, body),) = sender.sent
    assert "need attention" in subject
    assert "older sibling is never a swap target" in body


async def test_a_successor_that_fails_the_smoke_turn_is_never_written(caplog):
    transport = FakeTransport(
        existing=set(ALL_PINS) - {"claude-sonnet-4-6"},
        anthropic_pages=_anthropic_pages(("claude-sonnet-4-8", 500), ("claude-haiku-4-5", 200)),
        openai_listing=_openai_listing(("gpt-5.4", 400)),
        smoke_fail=True,
    )
    sender, writer, restarter = RecordingSender(), FakeWriter(), FakeRestarter()
    with (
        caplog.at_level(logging.WARNING, logger="app.runtime.model_currency"),
        pytest.raises(RuntimeError),
    ):
        await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW),
            transport_factory=lambda: transport,
            email_sender=sender,
            secret_writer=writer,
            restarter=restarter,
        )
    assert writer.applied == [] and restarter.restarts == 0
    assert len(transport.posts) == 1  # the smoke turn ran, once
    ((_, subject, body),) = sender.sent
    assert "need attention" in subject
    assert "failed a smoke turn" in body and "claude-sonnet-4-8" in body and "not swapped" in body
    assert any("model_currency_smoke_failed" in r.getMessage() for r in caplog.records)


async def test_an_injected_smoke_seam_replaces_the_transport_call():
    transport = FakeTransport(
        existing=set(ALL_PINS) - {"claude-sonnet-4-6"},
        anthropic_pages=_anthropic_pages(("claude-sonnet-4-8", 500), ("claude-haiku-4-5", 200)),
        openai_listing=_openai_listing(("gpt-5.4", 400)),
        smoke_fail=True,  # would fail if the transport smoke ran
    )
    seen: list[tuple[str, str]] = []

    async def smoke(*, provider: str, api_key: str, model_id: str) -> str | None:
        seen.append((provider, model_id))
        return None

    writer = FakeWriter()
    result = await run_model_currency_check(
        settings=live_settings(**_SETTINGS_KW),
        transport_factory=lambda: transport,
        email_sender=RecordingSender(),
        secret_writer=writer,
        restarter=FakeRestarter(),
        smoke=smoke,
    )
    assert seen == [("anthropic", "claude-sonnet-4-8")]
    assert transport.posts == []
    assert result["swapped"] and writer.applied


async def test_a_rejected_api_key_is_a_loud_alert_not_an_unreachable_shrug(caplog):
    transport = FakeTransport(
        existing=set(ALL_PINS),
        key_rejected=frozenset({"anthropic"}),
        openai_listing=_openai_listing(("gpt-5.4", 400)),
    )
    sender, writer = RecordingSender(), FakeWriter()
    with (
        caplog.at_level(logging.WARNING, logger="app.runtime.model_currency"),
        pytest.raises(RuntimeError),
    ):
        await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW),
            transport_factory=lambda: transport,
            email_sender=sender,
            secret_writer=writer,
            restarter=FakeRestarter(),
        )
    assert writer.applied == []
    ((_, subject, body),) = sender.sent
    assert "need attention" in subject
    assert "rejected the platform's API key" in body and "HTTP 401" in body
    assert any(MODEL_CURRENCY_KEY_REJECTED in r.getMessage() for r in caplog.records)
    # The OTHER provider's pins were still verified; nothing about them alarmed.
    assert "gpt-5.4" not in body
