"""Unit tests for the mode-aware LLM router factory (Architecture §3.4.3).

Proves the RUNTIME-UNCHANGED guarantee: mock mode builds the exact pre-cutover
EchoModel router; live mode builds the real providers from a (fake) platform secret
without importing keys from anywhere but the secret JSON, and with no network.
"""

from __future__ import annotations

import pytest
from app.config import Settings
from app.runtime.llm_router import LLMRouter
from app.runtime.provider_factory import build_llm_router
from app.runtime.providers import AnthropicProvider, OpenAIProvider
from app.runtime.testing import EchoModel

from tests.unit.live_settings import live_settings


def test_mock_mode_builds_echo_router() -> None:
    settings = Settings(integration_mode="mock")
    router = build_llm_router(settings)
    assert isinstance(router, LLMRouter)
    assert isinstance(router._primary, EchoModel)
    assert isinstance(router._fallback, EchoModel)


def test_default_mode_is_mock() -> None:
    # An unset integration_mode must default to mock (EchoModel behavior intact).
    settings = Settings()
    assert settings.integration_mode == "mock"
    router = build_llm_router(settings)
    assert isinstance(router._primary, EchoModel)


def test_live_mode_builds_real_providers_from_secret() -> None:
    settings = live_settings(
        ANTHROPIC_API_KEY="fake-anthropic-key", OPENAI_API_KEY="fake-openai-key"
    )
    router = build_llm_router(settings)
    assert isinstance(router._primary, AnthropicProvider)
    assert isinstance(router._fallback, OpenAIProvider)
    # Model IDs come from config (centralized, §7 #11).
    assert router._primary._model == settings.llm_anthropic_primary_model
    assert router._fallback._model == settings.llm_openai_fallback_model


def test_live_mode_missing_keys_raises() -> None:
    # Only the platform-wide secrets are provisioned: live mode now refuses to construct
    # Settings at all on an empty blob, so the provider keys must be the sole omission.
    settings = live_settings()
    with pytest.raises(RuntimeError) as exc:
        build_llm_router(settings)
    # The error names the missing keys but never echoes any value.
    assert "ANTHROPIC_API_KEY" in str(exc.value)
    assert "OPENAI_API_KEY" in str(exc.value)
