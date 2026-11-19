"""``llm_posture="live"``: real AI on a developer's own computer (the local Docker stack).

``docker compose up`` keeps ``integration_mode="mock"`` — local secret store, fake Stripe,
channel senders off, no AWS — and binds the REAL LLM and embeddings from whichever vendor
keys are in the platform secret. These tests pin the contract:

* the posture is development-only, needs at least one key, and parses the secret
  strictly (a typo'd blob is a boot failure, never a silent fall back to EchoModel);
* the router uses the keys that exist (one vendor may serve every leg) while the
  production router keeps its both-keys rule;
* embeddings are real only with an OpenAI key, and an Anthropic-only stack says so on
  ``/healthz`` (``embeddings:HashingEmbedder``);
* everything else AI-shaped (summaries, graph extraction, the relevance floor, the LLM
  cap store) follows the posture, and the delivery seams do NOT.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from app.config import LOCAL_ENVIRONMENT, PlatformSecretError, Settings, get_settings
from app.knowledge import graph_extract as ge
from app.knowledge.embeddings import HashingEmbedder, OpenAIEmbedder, get_embedder
from app.runtime import knowledge_retrieval, llm_cap, memory
from app.runtime.integration_status import degraded_bindings
from app.runtime.provider_factory import build_llm_router
from app.runtime.providers import AnthropicProvider, OpenAIProvider
from app.runtime.testing import EchoModel

from tests.unit.live_settings import live_settings

BOTH = {
    "ANTHROPIC_API_KEY": "fake-anthropic-key",  # pragma: allowlist secret
    "OPENAI_API_KEY": "fake-openai-key",  # pragma: allowlist secret
}
OPENAI_ONLY = {"OPENAI_API_KEY": BOTH["OPENAI_API_KEY"]}
ANTHROPIC_ONLY = {"ANTHROPIC_API_KEY": BOTH["ANTHROPIC_API_KEY"]}


def _local(keys: dict[str, str], **overrides: object) -> Settings:
    return Settings(
        llm_posture="live",
        platform_secret=json.dumps(keys),
        **overrides,  # type: ignore[arg-type]
    )


@pytest.fixture
def local_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Process settings as the Docker stack sets them (for code that reads
    ``get_settings()``); restored after the test."""
    monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", "mock")
    monkeypatch.setenv("LUCIEL_ENVIRONMENT", LOCAL_ENVIRONMENT)
    monkeypatch.setenv("LUCIEL_LLM_POSTURE", "live")
    monkeypatch.delenv("LUCIEL_GRAPH_EXTRACTOR", raising=False)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    ge.reset_extractor()
    yield monkeypatch
    get_settings.cache_clear()  # type: ignore[attr-defined]
    ge.reset_extractor()


def _use_keys(monkeypatch: pytest.MonkeyPatch, keys: dict[str, str]) -> None:
    monkeypatch.setenv("LUCIEL_PLATFORM_SECRET", json.dumps(keys))
    get_settings.cache_clear()  # type: ignore[attr-defined]
    ge.reset_extractor()


# --- boot rules ------------------------------------------------------------------- #


def test_the_default_posture_follows_integration_mode() -> None:
    settings = Settings()
    assert settings.llm_posture == "follow"
    assert settings.real_ai is False
    assert settings.real_embeddings is False
    assert live_settings(**BOTH).real_ai is True


def test_the_local_posture_is_refused_in_a_deployed_environment() -> None:
    with pytest.raises(RuntimeError, match="only for a developer's own computer"):
        _local(BOTH, environment="dev")


@pytest.mark.parametrize(
    "secret",
    ["{}", json.dumps({"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "   "})],
    ids=["no-keys", "blank-keys"],
)
def test_the_local_posture_refuses_to_start_without_an_ai_key(secret: str) -> None:
    """The Docker stack composes the secret from .env, so an unset key arrives as ''."""
    with pytest.raises(RuntimeError, match="needs an AI key"):
        Settings(llm_posture="live", platform_secret=secret)


def test_the_local_posture_parses_the_secret_strictly() -> None:
    with pytest.raises(PlatformSecretError):
        Settings(llm_posture="live", platform_secret='{"OPENAI_API_KEY": ')


def test_an_unknown_posture_is_refused() -> None:
    with pytest.raises(RuntimeError, match="LUCIEL_LLM_POSTURE must be one of"):
        Settings(llm_posture="sometimes")


# --- the router ------------------------------------------------------------------- #


def test_both_keys_build_the_production_shape() -> None:
    settings = _local(BOTH)
    router = build_llm_router(settings)
    assert isinstance(router._primary, AnthropicProvider)
    assert router._primary._model == settings.llm_anthropic_primary_model
    assert isinstance(router._fast, AnthropicProvider)
    assert router._fast._model == settings.llm_anthropic_fast_model
    assert isinstance(router._fallback, OpenAIProvider)
    assert router._fallback._model == settings.llm_openai_fallback_model


def test_an_openai_key_alone_serves_every_leg() -> None:
    settings = _local(OPENAI_ONLY)
    router = build_llm_router(settings)
    assert isinstance(router._primary, OpenAIProvider)
    assert router._fallback is router._primary
    assert router._fast is None  # every call goes to the primary


def test_an_anthropic_key_alone_serves_every_leg() -> None:
    settings = _local(ANTHROPIC_ONLY)
    router = build_llm_router(settings)
    assert isinstance(router._primary, AnthropicProvider)
    assert isinstance(router._fast, AnthropicProvider)
    assert router._fallback is router._primary


def test_the_production_router_keeps_its_both_keys_rule() -> None:
    """The one-key convenience is local only: live mode with one key still refuses."""
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        build_llm_router(live_settings(**OPENAI_ONLY))


def test_mock_mode_without_the_posture_stays_on_echo() -> None:
    router = build_llm_router(Settings(platform_secret=json.dumps(BOTH)))
    assert isinstance(router._primary, EchoModel)


# --- embeddings ------------------------------------------------------------------- #


def test_an_openai_key_gives_real_embeddings() -> None:
    settings = _local(OPENAI_ONLY)
    assert settings.real_embeddings is True
    embedder = get_embedder(settings)
    assert isinstance(embedder, OpenAIEmbedder)
    assert embedder._model == settings.knowledge_embedding_model


def test_an_anthropic_key_alone_keeps_the_hashing_embedder() -> None:
    settings = _local(ANTHROPIC_ONLY)
    assert settings.real_embeddings is False
    assert isinstance(get_embedder(settings), HashingEmbedder)


# --- everything that follows the posture ------------------------------------------ #


def test_the_graph_extractor_defaults_to_the_llm_under_the_local_posture() -> None:
    assert _local(BOTH).graph_extractor == "llm"


def test_the_seam_binds_the_llm_extractor_under_the_local_posture(
    local_env: pytest.MonkeyPatch,
) -> None:
    _use_keys(local_env, BOTH)
    assert isinstance(ge.get_extractor(), ge.LlmGraphExtractor)


def test_the_relevance_floor_follows_real_embeddings(local_env: pytest.MonkeyPatch) -> None:
    _use_keys(local_env, OPENAI_ONLY)
    assert knowledge_retrieval._relevance_floor_active() is True
    _use_keys(local_env, ANTHROPIC_ONLY)
    assert knowledge_retrieval._relevance_floor_active() is False


def test_session_summaries_use_the_real_llm(local_env: pytest.MonkeyPatch) -> None:
    import uuid

    _use_keys(local_env, ANTHROPIC_ONLY)
    router = memory._live_summary_router(uuid.uuid4())
    assert router is not None
    assert isinstance(router._primary, AnthropicProvider)


def test_the_llm_cap_counts_in_redis_when_the_llm_is_real(
    local_env: pytest.MonkeyPatch,
) -> None:
    _use_keys(local_env, OPENAI_ONLY)
    local_env.setattr(llm_cap, "_store", None)
    assert isinstance(llm_cap.get_llm_cap_store(), llm_cap.RedisLLMCapStore)
    local_env.setattr(llm_cap, "_store", None)


# --- what /healthz says ------------------------------------------------------------ #


def test_healthz_drops_echo_and_keeps_the_mock_delivery_seams(
    local_env: pytest.MonkeyPatch,
) -> None:
    _use_keys(local_env, BOTH)
    degraded = degraded_bindings()
    assert "llm:EchoModel" not in degraded
    assert "embeddings:HashingEmbedder" not in degraded
    # The delivery seams stay the mock-mode stand-ins under the local posture.
    assert "secret_store:LocalSecretStore" in degraded
    assert "stripe_gateway:FakeStripeGateway" in degraded


def test_healthz_names_hashed_knowledge_on_an_anthropic_only_stack(
    local_env: pytest.MonkeyPatch,
) -> None:
    _use_keys(local_env, ANTHROPIC_ONLY)
    degraded = degraded_bindings()
    assert "llm:EchoModel" not in degraded
    assert "embeddings:HashingEmbedder" in degraded


def test_healthz_reports_the_posture(local_env: pytest.MonkeyPatch) -> None:
    # Keys first: importing app.main builds the module-level app from the settings.
    _use_keys(local_env, BOTH)
    from app.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as client:
        body = client.get("/healthz").json()
    assert body["integrationMode"] == "mock"
    assert body["llmPosture"] == "live"
