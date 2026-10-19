"""Round 7 WP-6 item 7: embeddings get one retry, and a dead embedder is /healthz-loud.

``OpenAIEmbedder`` ran with ``max_retries=0`` and mapped ANY OpenAIError straight to
EmbeddingError, so one 429 burst at quota turned a visitor's turn into a hand-off (the
2026-09-06 walk). Now a 429/5xx gets ONE jittered retry (still no SDK retries, F101),
everything else fails at once, and a process whose last N embed calls all failed
reports ``embeddings:degraded`` in ``degraded_bindings`` — in live mode only.
"""

from __future__ import annotations

from typing import Any

import httpx
import openai
import pytest
from app.config import get_settings
from app.knowledge import embeddings
from app.knowledge.embeddings import (
    EMBED_DIM,
    EMBEDDINGS_DEGRADED_AFTER,
    EmbeddingError,
    OpenAIEmbedder,
    embeddings_degraded,
)
from app.runtime.integration_status import degraded_bindings

from tests.unit.live_settings import LIVE_PLATFORM_SECRET

_URL = "https://api.openai.com/v1/embeddings"


def _status_error(status: int) -> openai.APIStatusError:
    response = httpx.Response(status, request=httpx.Request("POST", _URL))
    cls = {
        429: openai.RateLimitError,
        500: openai.InternalServerError,
        503: openai.InternalServerError,
        400: openai.BadRequestError,
        401: openai.AuthenticationError,
    }.get(status, openai.APIStatusError)
    return cls("refused", response=response, body=None)


class _ScriptedEmbeddings:
    """``AsyncOpenAI().embeddings`` that raises the scripted errors, then answers."""

    def __init__(self, errors: list[Exception]) -> None:
        self._errors = list(errors)
        self.calls = 0

    async def create(self, *, model: str, input: list[str], dimensions: int) -> Any:  # noqa: A002
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        data = [
            type("Item", (), {"index": i, "embedding": [0.5] * dimensions})()
            for i in range(len(input))
        ]
        return type("Resp", (), {"data": data})()


def _embedder(fake: _ScriptedEmbeddings) -> OpenAIEmbedder:
    e = OpenAIEmbedder(api_key="sk-test", model="text-embedding-3-small")
    e._client = type("Client", (), {"embeddings": fake})()
    return e


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    sleeps: list[float] = []

    async def _no_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(embeddings.asyncio, "sleep", _no_sleep)
    embeddings.reset_embeddings_health()
    yield sleeps
    embeddings.reset_embeddings_health()


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_a_429_or_5xx_gets_one_jittered_retry(_fresh, status: int):
    fake = _ScriptedEmbeddings([_status_error(status)])
    vectors = await _embedder(fake).embed(["we open at nine"])
    assert len(vectors) == 1 and len(vectors[0]) == EMBED_DIM
    assert fake.calls == 2
    (delay,) = _fresh
    low, high = embeddings.EMBED_RETRY_DELAY_RANGE
    assert low <= delay <= min(high, get_settings().llm_timeout_seconds / 4)


async def test_the_retry_is_bounded_to_one():
    fake = _ScriptedEmbeddings([_status_error(429), _status_error(429), _status_error(429)])
    with pytest.raises(EmbeddingError, match="RateLimitError"):
        await _embedder(fake).embed(["hours?"])
    assert fake.calls == 2


@pytest.mark.parametrize(
    "error",
    [
        _status_error(400),
        _status_error(401),
        openai.APIConnectionError(request=httpx.Request("POST", _URL)),
        openai.APITimeoutError(request=httpx.Request("POST", _URL)),
    ],
)
async def test_other_failures_are_not_retried(_fresh, error: Exception):
    fake = _ScriptedEmbeddings([error])
    with pytest.raises(EmbeddingError):
        await _embedder(fake).embed(["hours?"])
    assert fake.calls == 1 and _fresh == []


async def test_n_consecutive_failures_flag_degraded_and_one_success_clears_it():
    for i in range(EMBEDDINGS_DEGRADED_AFTER):
        assert embeddings_degraded() is False, f"not degraded after {i} failures"
        with pytest.raises(EmbeddingError):
            await _embedder(_ScriptedEmbeddings([_status_error(401)])).embed(["x"])
    assert embeddings_degraded() is True
    await _embedder(_ScriptedEmbeddings([])).embed(["x"])
    assert embeddings_degraded() is False


async def test_a_success_between_failures_resets_the_streak():
    for _ in range(EMBEDDINGS_DEGRADED_AFTER - 1):
        with pytest.raises(EmbeddingError):
            await _embedder(_ScriptedEmbeddings([_status_error(401)])).embed(["x"])
    await _embedder(_ScriptedEmbeddings([_status_error(429)])).embed(["x"])  # retried → ok
    with pytest.raises(EmbeddingError):
        await _embedder(_ScriptedEmbeddings([_status_error(401)])).embed(["x"])
    assert embeddings_degraded() is False


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", "live")
    monkeypatch.setenv("LUCIEL_PLATFORM_SECRET", LIVE_PLATFORM_SECRET)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield
    get_settings.cache_clear()  # type: ignore[attr-defined]


async def test_healthz_names_a_degraded_embedder_in_live_mode(live):
    assert "embeddings:degraded" not in degraded_bindings()
    for _ in range(EMBEDDINGS_DEGRADED_AFTER):
        with pytest.raises(EmbeddingError):
            await _embedder(_ScriptedEmbeddings([_status_error(503), _status_error(503)])).embed(
                ["x"]
            )
    assert "embeddings:degraded" in degraded_bindings()
    await _embedder(_ScriptedEmbeddings([])).embed(["x"])
    assert "embeddings:degraded" not in degraded_bindings()


async def test_mock_mode_never_reports_the_embedder(monkeypatch):
    monkeypatch.setattr(embeddings, "_consecutive_failures", EMBEDDINGS_DEGRADED_AFTER)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    assert get_settings().integration_mode == "mock"
    assert "embeddings:degraded" not in degraded_bindings()
