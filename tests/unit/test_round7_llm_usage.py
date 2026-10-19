"""Round 7 WP-6 item 5: what every model call costs, per call and per turn.

Neither provider read ``resp.usage``: the platform paid for every token with no record
of how many, which model, or whether the system-prompt cache was hit. Each call now
emits LLMInputTokens / LLMOutputTokens / LLMCacheReadTokens / LLMCacheWriteTokens
(dims Provider + Model, the full dimension set so per-model cost is queryable), and
each customer turn logs one ``llm_usage`` line the round report aggregates into cost
per conversation. Fakes carry the SDKs' real usage shapes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import uuid
from types import SimpleNamespace

import pytest
from app.observability import metrics
from app.runtime import providers
from app.runtime.llm_router import LLMResponse, LLMRouter, LLMUsage, ModelClass, ProviderError
from app.runtime.providers import AnthropicProvider, OpenAIProvider


class _Create:
    def __init__(self, response: object) -> None:
        self._response = response

    async def create(self, **_kw: object) -> object:
        return self._response


def _anthropic_client(response: object) -> object:
    return SimpleNamespace(messages=_Create(response))


def _openai_client(response: object) -> object:
    return SimpleNamespace(chat=SimpleNamespace(completions=_Create(response)))


def _anthropic_response(**usage: int) -> object:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="We open at nine.")],
        usage=SimpleNamespace(**usage),
    )


def _openai_response(*, prompt: int, completion: int, cached: int | None) -> object:
    details = None if cached is None else SimpleNamespace(cached_tokens=cached, audio_tokens=0)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="We open at nine."))],
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            prompt_tokens_details=details,
        ),
    )


def _capture_metrics(monkeypatch) -> list[tuple[str, float, dict[str, str], bool]]:
    seen: list[tuple[str, float, dict[str, str], bool]] = []

    def fake_emit(name, value=1.0, *, unit="Count", dimensions=None, full_dimensions=False):
        seen.append((name, value, dict(dimensions or {}), full_dimensions))

    monkeypatch.setattr(providers, "emit_metric", fake_emit)
    return seen


# --- per call: both providers read usage and emit the four metrics ---------------- #


async def test_anthropic_usage_is_read_and_emitted_per_call(monkeypatch):
    seen = _capture_metrics(monkeypatch)
    p = AnthropicProvider(api_key="fake", model="claude-sonnet-4-6")
    p._client = _anthropic_client(
        _anthropic_response(
            input_tokens=120,
            output_tokens=30,
            cache_read_input_tokens=900,
            cache_creation_input_tokens=50,
        )
    )
    out = await p.complete("sys", [{"role": "user", "content": "hours?"}])
    assert out.usage == LLMUsage(
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=120,
        output_tokens=30,
        cache_read_tokens=900,
        cache_write_tokens=50,
    )
    dims = {"Provider": "anthropic", "Model": "claude-sonnet-4-6"}
    assert seen == [
        ("LLMInputTokens", 120.0, dims, True),
        ("LLMOutputTokens", 30.0, dims, True),
        ("LLMCacheReadTokens", 900.0, dims, True),
        ("LLMCacheWriteTokens", 50.0, dims, True),
    ]


async def test_anthropic_without_cache_fields_reports_zero_cache(monkeypatch):
    seen = _capture_metrics(monkeypatch)
    p = AnthropicProvider(api_key="fake", model="claude-haiku-4-5", name="anthropic_fast")
    p._client = _anthropic_client(
        _anthropic_response(
            input_tokens=40,
            output_tokens=8,
            cache_read_input_tokens=None,
            cache_creation_input_tokens=None,
        )
    )
    out = await p.complete("sys", [{"role": "user", "content": "hi"}])
    assert out.provider == "anthropic_fast"  # the routing name is unchanged
    assert out.usage is not None and out.usage.provider == "anthropic"  # the vendor
    assert (out.usage.cache_read_tokens, out.usage.cache_write_tokens) == (0, 0)
    assert [name for name, *_ in seen] == [name for name, _ in providers.TOKEN_METRICS]


async def test_openai_usage_separates_the_cached_prefix_from_uncached_input(monkeypatch):
    seen = _capture_metrics(monkeypatch)
    p = OpenAIProvider(api_key="fake", model="gpt-5.4")
    p._client = _openai_client(_openai_response(prompt=1000, completion=40, cached=768))
    out = await p.complete("sys", [{"role": "user", "content": "hours?"}])
    # prompt_tokens INCLUDES the cached part: uncached input is the difference.
    assert out.usage == LLMUsage(
        provider="openai",
        model="gpt-5.4",
        input_tokens=232,
        output_tokens=40,
        cache_read_tokens=768,
        cache_write_tokens=0,
    )
    dims = {"Provider": "openai", "Model": "gpt-5.4"}
    assert ("LLMCacheReadTokens", 768.0, dims, True) in seen
    assert ("LLMInputTokens", 232.0, dims, True) in seen


async def test_openai_without_prompt_details_has_no_cache_read(monkeypatch):
    _capture_metrics(monkeypatch)
    p = OpenAIProvider(api_key="fake", model="gpt-5.4")
    p._client = _openai_client(_openai_response(prompt=300, completion=12, cached=None))
    out = await p.complete("sys", [{"role": "user", "content": "hi"}])
    assert out.usage is not None
    assert (out.usage.input_tokens, out.usage.cache_read_tokens) == (300, 0)


async def test_a_response_without_usage_emits_nothing(monkeypatch):
    seen = _capture_metrics(monkeypatch)
    p = OpenAIProvider(api_key="fake", model="gpt-5.4")
    p._client = _openai_client(
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
    )
    out = await p.complete("sys", [{"role": "user", "content": "hi"}])
    assert out.usage is None and seen == []


def test_token_metrics_publish_the_full_dimension_set(monkeypatch, capsys):
    """The default set is [Environment] (WP-8); these four add the full combination
    so Sum(LLMInputTokens) by Model is a real CloudWatch metric."""
    monkeypatch.setenv("LUCIEL_PLANE", "data")
    for handler in list(logging.getLogger(metrics.METRICS_LOGGER_NAME).handlers):
        logging.getLogger(metrics.METRICS_LOGGER_NAME).removeHandler(handler)
    providers.emit_usage_metrics(
        LLMUsage(provider="anthropic", model="claude-sonnet-4-6", input_tokens=7)
    )
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert [doc["_aws"]["CloudWatchMetrics"][0]["Metrics"][0]["Name"] for doc in lines] == [
        "LLMInputTokens",
        "LLMOutputTokens",
        "LLMCacheReadTokens",
        "LLMCacheWriteTokens",
    ]
    first = lines[0]
    assert first["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [
        ["Environment"],
        ["Environment", "Plane", "Provider", "Model"],
    ]
    assert first["Provider"] == "anthropic" and first["Model"] == "claude-sonnet-4-6"
    assert first["LLMInputTokens"] == 7.0


# --- per turn: one llm_usage line --------------------------------------------------- #


class _Priced:
    """A provider double that reports usage like the live ones do."""

    def __init__(self, name: str, *, vendor: str, model: str, fail: bool = False) -> None:
        self.name = name
        self._vendor = vendor
        self._model = model
        self._fail = fail

    async def complete(self, system_prompt, messages, *, tools=None) -> LLMResponse:
        if self._fail:
            raise ProviderError(f"{self.name}_down")
        return LLMResponse(
            text="ok",
            provider=self.name,
            model_class=ModelClass.primary,
            confidence=1.0,
            usage=LLMUsage(
                provider=self._vendor,
                model=self._model,
                input_tokens=100,
                output_tokens=20,
                cache_read_tokens=900,
                cache_write_tokens=0,
            ),
        )


def _usage_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("llm_usage ")]


async def test_a_turn_logs_one_usage_line_summing_its_calls(caplog):
    router = LLMRouter(
        primary=_Priced("anthropic", vendor="anthropic", model="claude-sonnet-4-6"),
        fallback=_Priced("openai", vendor="openai", model="gpt-5.4"),
    )
    with caplog.at_level(logging.INFO, logger="app.runtime.llm_router"):
        with router.turn():
            for _ in range(3):  # a tool-using turn: three model calls
                await router.complete("sys", [{"role": "user", "content": "hi"}])
    (line,) = _usage_lines(caplog)
    assert line == (
        "llm_usage admin_id_hash=- provider=anthropic model=claude-sonnet-4-6 calls=3 "
        "input=300 output=60 cache_read=2700 cache_write=0"
    )


async def test_the_usage_line_names_the_tenant_by_hash_only(caplog, monkeypatch):
    import app.runtime.llm_cap as llm_cap

    monkeypatch.setattr(llm_cap, "_store", llm_cap.InMemoryLLMCapStore())
    admin = uuid.uuid4()
    router = LLMRouter(
        primary=_Priced("anthropic", vendor="anthropic", model="claude-sonnet-4-6", fail=True),
        fallback=_Priced("openai", vendor="openai", model="gpt-5.4"),
        admin_id=admin,
    )
    with caplog.at_level(logging.INFO, logger="app.runtime.llm_router"):
        with router.turn():
            await router.complete("sys", [{"role": "user", "content": "hi"}])
    (line,) = _usage_lines(caplog)
    # The primary failed: the turn's cost is the fallback's, under its own model.
    assert "provider=openai model=gpt-5.4 calls=1" in line
    assert f"admin_id_hash={hashlib.sha256(str(admin).encode()).hexdigest()[:16]}" in line
    assert str(admin) not in line


async def test_calls_outside_a_turn_log_no_usage_line(caplog):
    router = LLMRouter(
        primary=_Priced("anthropic", vendor="anthropic", model="claude-sonnet-4-6"),
        fallback=_Priced("openai", vendor="openai", model="gpt-5.4"),
    )
    with caplog.at_level(logging.INFO, logger="app.runtime.llm_router"):
        await router.complete("sys", [{"role": "user", "content": "hi"}])
    assert _usage_lines(caplog) == []


@pytest.mark.parametrize("raised", [True, False])
async def test_a_turn_that_ends_in_an_error_still_logs_what_it_spent(caplog, raised):
    router = LLMRouter(
        primary=_Priced("anthropic", vendor="anthropic", model="claude-sonnet-4-6"),
        fallback=_Priced("openai", vendor="openai", model="gpt-5.4"),
    )
    with caplog.at_level(logging.INFO, logger="app.runtime.llm_router"):
        with pytest.raises(RuntimeError) if raised else contextlib.nullcontext():
            with router.turn():
                await router.complete("sys", [{"role": "user", "content": "hi"}])
                if raised:
                    raise RuntimeError("tool dispatch blew up")
    (line,) = _usage_lines(caplog)
    assert "calls=1" in line
