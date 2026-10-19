"""Round 7 WP-6: the model-currency watchdog can actually remediate what it detects.

Each test models a live condition the 26834f1 review found:

  1. the OpenAI successor smoke posted ``max_tokens: 8`` — GPT-5.x rejects that
     parameter (400 unsupported_parameter), and even under the right name 8 tokens is
     spent on reasoning before any answer, so a retired fallback pin could never swap;
  2. ``gpt-5`` and ``gpt-5.4`` derived different families, so a retired ``gpt-5``
     never paired with ``gpt-5.4``;
  3. a swap whose ECS restart failed (or was skipped) logged a WARNING and exited 0 —
     the running fleet kept calling the retired model and nothing paged anyone;
  8. the embedding pin is alert-only, and its alert names the re-embed path that
     actually exists (``app/knowledge/service.py`` via the knowledge_sync sweep).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from app.runtime import model_currency
from app.runtime.model_currency import (
    MODEL_CURRENCY_ALERT,
    SMOKE_MAX_COMPLETION_TOKENS_OPENAI,
    SMOKE_PROMPT,
    model_family,
    pick_newer_successor,
    run_model_currency_check,
    smoke_request,
)

from tests.unit.live_settings import live_settings
from tests.unit.test_model_currency import (
    ALL_PINS,
    FakeRestarter,
    FakeTransport,
    FakeWriter,
    RecordingSender,
    _anthropic_pages,
    _openai_listing,
    _Resp,
)

_SETTINGS_KW = {"ANTHROPIC_API_KEY": "sk-ant-fake", "OPENAI_API_KEY": "sk-oai-fake"}

# The smallest completion budget a GPT-5.x reasoning model needs before it writes
# any visible answer in this fake (its hidden reasoning comes first).
_REASONING_BUDGET = 64


@pytest.fixture(autouse=True)
def _no_env_pins(monkeypatch):
    for pin in model_currency.PINS:
        monkeypatch.delenv(pin.env_var, raising=False)
    monkeypatch.delenv("LUCIEL_PLATFORM_SECRET_ARN", raising=False)
    monkeypatch.delenv("LUCIEL_ECS_CLUSTER", raising=False)
    monkeypatch.delenv("LUCIEL_ECS_SERVICES", raising=False)


class Gpt5ChatTransport(FakeTransport):
    """The live GPT-5.x chat-completions contract on the smoke POST: ``max_tokens``
    is a 400 unsupported_parameter, and a ``max_completion_tokens`` ceiling below
    the reasoning budget returns 200 with EMPTY content and finish_reason=length."""

    async def post(self, url: str, *, json: Any = None, headers: Any = None):
        self.posts.append((url, json))
        if "openai" not in url:
            return await super().post(url, json=json, headers=headers)
        if "max_tokens" in (json or {}):
            return _Resp(
                {
                    "error": {
                        "message": "Unsupported parameter: 'max_tokens' is not supported "
                        "with this model. Use 'max_completion_tokens' instead.",
                        "type": "invalid_request_error",
                        "code": "unsupported_parameter",
                    }
                },
                status=400,
            )
        budget = int((json or {}).get("max_completion_tokens") or 0)
        if budget < _REASONING_BUDGET:
            return _Resp({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
        return _Resp({"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})


# --- item 1: the smoke turn speaks each provider's own contract ------------------ #


def test_smoke_payload_is_exact_per_provider():
    url, payload, headers = smoke_request("openai", api_key="sk-oai-fake", model_id="gpt-5.6")
    assert url == "https://api.openai.com/v1/chat/completions"
    assert payload == {
        "model": "gpt-5.6",
        "max_completion_tokens": SMOKE_MAX_COMPLETION_TOKENS_OPENAI,
        "messages": [{"role": "user", "content": SMOKE_PROMPT}],
    }
    assert "max_tokens" not in payload  # GPT-5.x answers 400 unsupported_parameter
    assert SMOKE_MAX_COMPLETION_TOKENS_OPENAI >= 4 * _REASONING_BUDGET
    assert headers["Authorization"] == "Bearer sk-oai-fake"

    url, payload, headers = smoke_request(
        "anthropic", api_key="sk-ant-fake", model_id="claude-sonnet-4-8"
    )
    assert url == "https://api.anthropic.com/v1/messages"
    assert payload == {
        "model": "claude-sonnet-4-8",
        "max_tokens": model_currency.SMOKE_MAX_TOKENS_ANTHROPIC,
        "messages": [{"role": "user", "content": SMOKE_PROMPT}],
    }
    assert "max_completion_tokens" not in payload
    assert headers["x-api-key"] == "sk-ant-fake" and headers["anthropic-version"]


async def test_the_default_smoke_posts_the_exact_payload_through_the_transport():
    transport = Gpt5ChatTransport(existing=set())
    failure = await model_currency._default_smoke(
        transport, provider="openai", api_key="sk-oai-fake", model_id="gpt-5.6"
    )
    assert failure is None
    ((url, payload),) = transport.posts
    assert url.endswith("/v1/chat/completions")
    assert payload["max_completion_tokens"] == SMOKE_MAX_COMPLETION_TOKENS_OPENAI
    assert "max_tokens" not in payload

    failure = await model_currency._default_smoke(
        transport, provider="anthropic", api_key="sk-ant-fake", model_id="claude-sonnet-4-8"
    )
    assert failure is None
    assert transport.posts[1][1]["max_tokens"] == model_currency.SMOKE_MAX_TOKENS_ANTHROPIC


async def test_a_200_with_an_empty_completion_fails_the_smoke():
    """An exhausted budget is a 200 with nothing in it — never a passing successor."""

    class _Empty(FakeTransport):
        async def post(self, url: str, *, json: Any = None, headers: Any = None):
            self.posts.append((url, json))
            if "anthropic" in url:
                return _Resp({"content": [], "stop_reason": "max_tokens"})
            return _Resp({"choices": [{"message": {"content": None}, "finish_reason": "length"}]})

    transport = _Empty(existing=set())
    openai_failure = await model_currency._default_smoke(
        transport, provider="openai", api_key="k", model_id="gpt-5.6"
    )
    anthropic_failure = await model_currency._default_smoke(
        transport, provider="anthropic", api_key="k", model_id="claude-sonnet-4-8"
    )
    assert openai_failure == "empty completion (stop=length)"
    assert anthropic_failure == "empty completion (stop=max_tokens)"


async def test_a_retired_openai_fallback_pin_now_swaps_to_its_gpt5_successor():
    """The live condition end to end: gpt-5.4 retires, gpt-5.6 is listed, and the
    successor's smoke must pass under the GPT-5.x contract for the swap to happen."""
    transport = Gpt5ChatTransport(
        existing=set(ALL_PINS) - {"gpt-5.4"},
        openai_listing=_openai_listing(("gpt-5.4", 400), ("gpt-5.6", 900)),
    )
    writer, restarter = FakeWriter(), FakeRestarter()
    result = await run_model_currency_check(
        settings=live_settings(**_SETTINGS_KW),
        transport_factory=lambda: transport,
        email_sender=RecordingSender(),
        secret_writer=writer,
        restarter=restarter,
    )
    assert result["swapped"] == [
        {"setting": "llm_openai_fallback_model", "old": "gpt-5.4", "new": "gpt-5.6"}
    ]
    assert writer.applied[0]["LLM_OPENAI_FALLBACK_MODEL"] == "gpt-5.6"
    assert restarter.restarts == 1
    ((url, payload),) = transport.posts
    assert "openai" in url and "max_tokens" not in payload


async def test_an_openai_successor_that_answers_empty_is_not_swapped():
    """If the provider still spends the whole budget on reasoning, the alert names it
    instead of pinning a model that answers nothing."""

    class _AlwaysEmpty(Gpt5ChatTransport):
        async def post(self, url: str, *, json: Any = None, headers: Any = None):
            self.posts.append((url, json))
            return _Resp({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})

    transport = _AlwaysEmpty(
        existing=set(ALL_PINS) - {"gpt-5.4"},
        openai_listing=_openai_listing(("gpt-5.4", 400), ("gpt-5.6", 900)),
    )
    sender, writer = RecordingSender(), FakeWriter()
    with pytest.raises(RuntimeError, match="model_currency"):
        await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW),
            transport_factory=lambda: transport,
            email_sender=sender,
            secret_writer=writer,
            restarter=FakeRestarter(),
        )
    assert writer.applied == []
    ((_to, _subject, body),) = sender.sent
    assert "failed a smoke turn (empty completion (stop=length))" in body


# --- item 2: dotted and bare-integer ids derive the same family ------------------- #


@pytest.mark.parametrize(
    ("model_id", "family"),
    [
        ("claude-sonnet-4-6", "claude-sonnet"),
        ("claude-sonnet-5", "claude-sonnet"),
        ("gpt-5", "gpt-5"),  # used to derive "gpt" — never paired with gpt-5.4
        ("gpt-5.4", "gpt-5"),
        ("gpt-5-mini", "gpt-5-mini"),
        ("gpt-5.4-mini", "gpt-5-mini"),
        ("gpt-5-2025-08-07", "gpt-5"),
        ("gpt-5.4-2026-03-05", "gpt-5"),
        ("gpt-5-mini-2025-08-07", "gpt-5-mini"),
        ("claude-haiku-4-5-20251001", "claude-haiku"),
        ("o3-2025-04-16", "o3"),
        ("gpt-4o", "gpt-4o"),
        ("gpt-6", "gpt-6"),
        ("text-embedding-3-small", "text-embedding-3-small"),
    ],
)
def test_model_family_table(model_id: str, family: str):
    assert model_family(model_id) == family


def test_a_retired_bare_generation_pairs_with_its_dotted_successor():
    listing = [{"id": "gpt-5.4", "created": 300}, {"id": "gpt-6", "created": 900}]
    assert pick_newer_successor("gpt-5", listing) == "gpt-5.4"
    # ...and never the other way round: the bare generation is OLDER than 5.4.
    assert pick_newer_successor("gpt-5.4", [{"id": "gpt-5", "created": 999}]) is None
    # A dated snapshot of the bare generation is not a newer version either.
    assert pick_newer_successor("gpt-5.4", [{"id": "gpt-5-2025-08-07", "created": 999}]) is None
    # A different tier never stands in for the generation.
    assert pick_newer_successor("gpt-5", [{"id": "gpt-5-mini", "created": 999}]) is None
    # Claude's family spans majors (the existing doctrine): 4-6 → 5 pairs.
    assert (
        pick_newer_successor("claude-sonnet-4-6", [{"id": "claude-sonnet-5", "created": 900}])
        == "claude-sonnet-5"
    )


def test_another_tiers_release_date_does_not_age_the_pin():
    """``gpt-5-mini`` starts with ``gpt-5-``; counting it as a snapshot of ``gpt-5``
    made every gpt-5.x released before the mini look older than the pin."""
    listing = [
        {"id": "gpt-5-2025-08-07", "created": 100},  # the pin's own dated snapshot
        {"id": "gpt-5.4", "created": 300},
        {"id": "gpt-5-mini", "created": 500},
    ]
    assert pick_newer_successor("gpt-5", listing) == "gpt-5.4"


async def test_a_retired_gpt5_fallback_pin_swaps_to_gpt54():
    transport = Gpt5ChatTransport(
        existing=set(ALL_PINS) - {"gpt-5.4"},  # the gpt-5 pin 404s (it was never listed)
        openai_listing=_openai_listing(("gpt-5-2025-08-07", 100), ("gpt-5.4", 400)),
    )
    writer = FakeWriter()
    result = await run_model_currency_check(
        settings=live_settings(**_SETTINGS_KW, LLM_OPENAI_FALLBACK_MODEL="gpt-5"),
        transport_factory=lambda: transport,
        email_sender=RecordingSender(),
        secret_writer=writer,
        restarter=FakeRestarter(),
    )
    assert result["swapped"] == [
        {"setting": "llm_openai_fallback_model", "old": "gpt-5", "new": "gpt-5.4"}
    ]


# --- item 3: a swap without its restart is an alert, every day until it happens ---- #


class FailingRestarter:
    def __init__(self) -> None:
        self.attempts = 0

    async def restart(self) -> None:
        self.attempts += 1
        raise PermissionError("ecs:UpdateService denied")


def _retired_primary_transport() -> FakeTransport:
    return FakeTransport(
        existing=set(ALL_PINS) - {"claude-sonnet-4-6"},
        anthropic_pages=_anthropic_pages(("claude-sonnet-4-8", 500), ("claude-haiku-4-5", 200)),
        openai_listing=_openai_listing(("gpt-5.4", 400)),
    )


@pytest.mark.parametrize("restart_mode", ["failed", "skipped"])
async def test_a_swap_whose_restart_fails_or_is_skipped_alerts_and_raises(caplog, restart_mode):
    """The live condition: the pin moved in the secret, but ECS injects the secret at
    task start — every running task keeps calling the retired model. That used to be a
    WARNING and exit 0, which the ERROR-only SweepErrors metric filter never saw."""
    restarter = FailingRestarter() if restart_mode == "failed" else None
    sender, writer = RecordingSender(), FakeWriter()
    with (
        caplog.at_level(logging.WARNING, logger="app.runtime.model_currency"),
        pytest.raises(RuntimeError, match="model_currency"),
    ):
        await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW),
            transport_factory=_retired_primary_transport,
            email_sender=sender,
            secret_writer=writer,
            restarter=restarter,
        )
    # The pin moved and the restart is recorded as still owed — never marked done.
    (applied,) = writer.applied
    assert applied["LLM_ANTHROPIC_PRIMARY_MODEL"] == "claude-sonnet-4-8"
    assert json.loads(applied["LLM_MODEL_UPDATED"])["restart"] == "pending"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors and MODEL_CURRENCY_ALERT in errors[-1].getMessage()
    assert "have NOT restarted" in errors[-1].getMessage()
    swap_mail, alert_mail = sender.sent
    assert "could NOT restart" in swap_mail[2] and "No action is required" not in swap_mail[2]
    assert "need attention" in alert_mail[1]
    assert "still call the retired 'claude-sonnet-4-6'" in alert_mail[2]
    if restart_mode == "skipped":
        assert "LUCIEL_ECS_CLUSTER" in alert_mail[2]
    else:
        assert restarter is not None and restarter.attempts == 1


def _owed_record() -> dict[str, str]:
    return {
        "at": "2026-10-18T06:00:00+00:00",
        "setting": "llm_anthropic_primary_model",
        "from": "claude-sonnet-4-6",
        "to": "claude-sonnet-4-8",
        "restart": "pending",
    }


def _after_swap_transport() -> FakeTransport:
    # Tomorrow the secret holds the successor, which exists: every retrieve is healthy.
    return FakeTransport(existing=(set(ALL_PINS) - {"claude-sonnet-4-6"}) | {"claude-sonnet-4-8"})


async def test_the_next_run_alerts_again_while_the_restart_is_still_owed(caplog):
    """Tomorrow's sweep reads the NEW pin (it exists, so the retrieve is healthy) —
    only the swap record knows the fleet never restarted. It retries and alerts."""
    restarter = FailingRestarter()
    sender, writer = RecordingSender(), FakeWriter()
    with (
        caplog.at_level(logging.ERROR, logger="app.runtime.model_currency"),
        pytest.raises(RuntimeError, match="model_currency"),
    ):
        await run_model_currency_check(
            settings=live_settings(
                **_SETTINGS_KW,
                LLM_ANTHROPIC_PRIMARY_MODEL="claude-sonnet-4-8",
                LLM_MODEL_UPDATED=json.dumps(_owed_record()),
            ),
            transport_factory=_after_swap_transport,
            email_sender=sender,
            secret_writer=writer,
            restarter=restarter,
        )
    assert restarter.attempts == 1 and writer.applied == []
    ((_to, _subject, body),) = sender.sent
    assert "'claude-sonnet-4-6' → 'claude-sonnet-4-8'" in body and "repeats daily" in body
    assert any(MODEL_CURRENCY_ALERT in r.getMessage() for r in caplog.records)


async def test_an_owed_restart_that_succeeds_settles_the_record_and_stops_alerting():
    restarter, writer, sender = FakeRestarter(), FakeWriter(), RecordingSender()
    result = await run_model_currency_check(
        settings=live_settings(
            **_SETTINGS_KW,
            LLM_ANTHROPIC_PRIMARY_MODEL="claude-sonnet-4-8",
            LLM_MODEL_UPDATED=json.dumps(_owed_record()),
        ),
        transport_factory=_after_swap_transport,
        email_sender=sender,
        secret_writer=writer,
        restarter=restarter,
    )
    assert restarter.restarts == 1
    (marked,) = writer.applied
    assert set(marked) == {"LLM_MODEL_UPDATED"}
    assert json.loads(marked["LLM_MODEL_UPDATED"]) == {**_owed_record(), "restart": "done"}
    assert result["swapped"] == [] and sender.sent == []


async def test_a_settled_or_legacy_swap_record_owes_nothing():
    for record in (
        {**_owed_record(), "restart": "done"},
        {k: v for k, v in _owed_record().items() if k != "restart"},  # pre-WP-6 record
    ):
        restarter = FailingRestarter()
        result = await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW, LLM_MODEL_UPDATED=json.dumps(record)),
            transport_factory=lambda: FakeTransport(existing=set(ALL_PINS)),
            email_sender=RecordingSender(),
            secret_writer=FakeWriter(),
            restarter=restarter,
        )
        assert restarter.attempts == 0 and result["swapped"] == []


# --- item 8: a retired embedding model alerts, and the alert names the real path --- #


async def test_a_retired_embedding_model_alerts_with_the_reembed_runbook(caplog):
    """The embedding pin 404s (OpenAI retired text-embedding-3-small): never swapped —
    stored vectors are model-specific — so the run logs MODEL_CURRENCY_ALERT at ERROR,
    raises (the SweepErrors alarm), writes nothing, restarts nothing, and the alert
    tells the operator the exact re-embed path."""
    transport = FakeTransport(
        existing=set(ALL_PINS) - {"text-embedding-3-small"},
        openai_listing=_openai_listing(("gpt-5.4", 400), ("text-embedding-4-small", 900)),
    )
    sender, writer, restarter = RecordingSender(), FakeWriter(), FakeRestarter()
    with (
        caplog.at_level(logging.ERROR, logger="app.runtime.model_currency"),
        pytest.raises(RuntimeError, match="model_currency"),
    ):
        await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW),
            transport_factory=lambda: transport,
            email_sender=sender,
            secret_writer=writer,
            restarter=restarter,
        )
    assert writer.applied == [] and restarter.restarts == 0 and transport.posts == []
    assert transport.listing_calls("openai") == []  # no successor is even looked up
    (error,) = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert MODEL_CURRENCY_ALERT in error.getMessage()
    assert "knowledge_embedding_model = 'text-embedding-3-small' is gone" in error.getMessage()
    ((_to, subject, body),) = sender.sent
    assert "need attention" in subject
    for step in (
        "LUCIEL_KNOWLEDGE_EMBEDDING_MODEL",
        "1536 dimensions",
        "knowledge_sync",
        "reembed_outdated_chunks",
        "verify_chunk_embeddings",
        "migration 0065",
    ):
        assert step in body, step


def test_the_documented_reembed_path_exists_and_the_sweep_runs_it():
    """The runbook names functions; they must exist and the knowledge_sync sweep must
    call them, or the alert sends the operator down a path that is not there."""
    import inspect

    from app.knowledge import service, sweep

    assert inspect.iscoroutinefunction(service.reembed_outdated_chunks)
    assert inspect.iscoroutinefunction(service.verify_chunk_embeddings)
    sweep_source = inspect.getsource(sweep)
    assert "service.reembed_outdated_chunks(" in sweep_source
    assert "service.verify_chunk_embeddings(" in sweep_source
    # The re-embed pass selects on the recorded model label, so moving the pin is
    # what makes every older vector a candidate.
    assert "embedding_model.is_distinct_from(current)" in inspect.getsource(
        service.reembed_outdated_chunks
    )
    # Migration 0065 is the verification column the second pass converges on.
    assert "embedding_verified_at.is_(None)" in inspect.getsource(service.verify_chunk_embeddings)
    (pin,) = [p for p in model_currency.PINS if p.setting == "knowledge_embedding_model"]
    assert pin.swappable is False
    assert pin.alert_only_reason == model_currency.EMBEDDING_RETIREMENT_RUNBOOK


# --- item 9: the ElevenLabs ids are pinned, rotatable and watched (alert-only) ------ #

_EL_KEY = {"ELEVENLABS_API_KEY": "el-fake"}
_VOICE = "21m00Tcm4TlvDq8ikWAM"
_EL_MODEL = "eleven_multilingual_v2"


class ElevenLabsTransport(FakeTransport):
    """The two ElevenLabs reads the watchdog makes: ``GET /v1/voices/{id}`` (400 with
    a voice_not_found detail for a deleted voice) and the ``GET /v1/models`` list."""

    def __init__(self, *, voices: set[str], models: set[str], **kw: Any) -> None:
        super().__init__(**kw)
        self._voices = voices
        self._models = models
        self.elevenlabs_calls: list[tuple[str, dict | None]] = []

    async def get(self, url: str, *, params=None, headers=None):
        if "api.elevenlabs.io" not in url:
            return await super().get(url, params=params, headers=headers)
        self.elevenlabs_calls.append((url, headers))
        if url.endswith("/v1/models"):
            return _Resp([{"model_id": m, "name": m} for m in sorted(self._models)])  # type: ignore[arg-type]
        voice_id = url.rsplit("/", 1)[1]
        if voice_id in self._voices:
            return _Resp({"voice_id": voice_id, "name": "Rachel"})
        return _Resp(
            {
                "detail": {
                    "status": "voice_not_found",
                    "message": f"A voice with the voice_id {voice_id} was not found.",
                }
            },
            status=400,
        )


def test_the_elevenlabs_ids_are_registered_alert_only():
    pins = {p.setting: p for p in model_currency.PINS if p.provider == "elevenlabs"}
    assert set(pins) == {"elevenlabs_voice_id", "elevenlabs_model_id"}
    assert all(p.swappable is False for p in pins.values())
    assert pins["elevenlabs_voice_id"].kind == "voice"
    assert pins["elevenlabs_voice_id"].blob_key == "ELEVENLABS_VOICE_ID"
    assert pins["elevenlabs_model_id"].blob_key == "ELEVENLABS_MODEL_ID"


async def test_healthy_elevenlabs_ids_are_checked_with_a_voice_get_and_the_model_list():
    transport = ElevenLabsTransport(
        existing=set(ALL_PINS), voices={_VOICE}, models={_EL_MODEL, "eleven_flash_v2_5"}
    )
    result = await run_model_currency_check(
        settings=live_settings(**_SETTINGS_KW, **_EL_KEY),
        transport_factory=lambda: transport,
        email_sender=RecordingSender(),
        secret_writer=FakeWriter(),
        restarter=FakeRestarter(),
    )
    assert {"elevenlabs_voice_id", "elevenlabs_model_id"} <= set(result["checked"])
    assert result["skipped"] == []
    urls = [url for url, _ in transport.elevenlabs_calls]
    assert urls == [
        f"https://api.elevenlabs.io/v1/voices/{_VOICE}",
        "https://api.elevenlabs.io/v1/models",
    ]
    assert all(h == {"xi-api-key": "el-fake"} for _, h in transport.elevenlabs_calls)


async def test_a_deleted_voice_alerts_and_is_never_swapped(caplog):
    transport = ElevenLabsTransport(
        existing=set(ALL_PINS), voices={"some-other-voice"}, models={_EL_MODEL}
    )
    sender, writer = RecordingSender(), FakeWriter()
    with (
        caplog.at_level(logging.ERROR, logger="app.runtime.model_currency"),
        pytest.raises(RuntimeError, match="model_currency"),
    ):
        await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW, **_EL_KEY),
            transport_factory=lambda: transport,
            email_sender=sender,
            secret_writer=writer,
            restarter=FakeRestarter(),
        )
    assert writer.applied == []
    ((_to, _subject, body),) = sender.sent
    assert f"elevenlabs_voice_id = '{_VOICE}' is gone from elevenlabs" in body
    assert "ELEVENLABS_VOICE_ID" in body and "<Say>" in body
    assert "elevenlabs_model_id" not in body  # the model is still listed
    assert any(MODEL_CURRENCY_ALERT in r.getMessage() for r in caplog.records)


async def test_a_delisted_tts_model_alerts(caplog):
    transport = ElevenLabsTransport(
        existing=set(ALL_PINS), voices={_VOICE}, models={"eleven_flash_v2_5"}
    )
    sender = RecordingSender()
    with pytest.raises(RuntimeError, match="model_currency"):
        await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW, **_EL_KEY, ELEVENLABS_MODEL_ID=_EL_MODEL),
            transport_factory=lambda: transport,
            email_sender=sender,
            secret_writer=FakeWriter(),
            restarter=FakeRestarter(),
        )
    ((_to, _subject, body),) = sender.sent
    assert f"elevenlabs_model_id = '{_EL_MODEL}' is gone from elevenlabs" in body
    assert "ELEVENLABS_MODEL_ID" in body


async def test_without_an_elevenlabs_key_the_ids_are_skipped_not_alerted():
    """Premium voice audio is opt-in; a platform secret without the vendor key has
    nothing to verify, and that is not a misconfiguration of the watchdog."""
    transport = ElevenLabsTransport(existing=set(ALL_PINS), voices=set(), models=set())
    result = await run_model_currency_check(
        settings=live_settings(**_SETTINGS_KW),
        transport_factory=lambda: transport,
        email_sender=RecordingSender(),
        secret_writer=FakeWriter(),
        restarter=FakeRestarter(),
    )
    assert result["skipped"] == ["elevenlabs_voice_id", "elevenlabs_model_id"]
    assert transport.elevenlabs_calls == []


async def test_a_rotated_voice_id_is_what_the_watchdog_checks():
    transport = ElevenLabsTransport(existing=set(ALL_PINS), voices={"v-new"}, models={_EL_MODEL})
    result = await run_model_currency_check(
        settings=live_settings(**_SETTINGS_KW, **_EL_KEY, ELEVENLABS_VOICE_ID="v-new"),
        transport_factory=lambda: transport,
        email_sender=RecordingSender(),
        secret_writer=FakeWriter(),
        restarter=FakeRestarter(),
    )
    assert "elevenlabs_voice_id" in result["checked"]
    assert transport.elevenlabs_calls[0][0].endswith("/v1/voices/v-new")
