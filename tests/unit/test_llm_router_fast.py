"""Intra-session fast/cheap model routing (Architecture §3.4.3 / §7 #9).

``should_route_fast()`` existed but was never called anywhere — this proves it is
now wired into ``LLMRouter.complete()`` as strictly ADDITIVE behavior: existing
callers (notably ``orchestrator.py``'s ``self.router.complete(self.system_prompt,
messages)``, which passes none of the new hint kwargs) must be byte-for-byte
unaffected, and the Anthropic-primary/OpenAI-fallback failover doctrine must be
unchanged regardless of whether a fast provider is bound.
"""

from __future__ import annotations

import json

import pytest
from app.config import Settings
from app.runtime.llm_router import (
    CONTEXT_TOKEN_BUDGET_FAST,
    LLMRouter,
    ModelClass,
    should_route_fast,
)
from app.runtime.provider_factory import build_llm_router
from app.runtime.providers import AnthropicProvider
from app.runtime.testing import EchoModel

pytestmark = [pytest.mark.asyncio]


def test_should_route_fast_gate_requires_all_three_conditions() -> None:
    # All conditions satisfied -> True.
    assert should_route_fast(requires_tools=False, context_tokens=100, complexity=0.1)
    # Any one condition failing -> False.
    assert not should_route_fast(requires_tools=True, context_tokens=100, complexity=0.1)
    assert not should_route_fast(
        requires_tools=False, context_tokens=CONTEXT_TOKEN_BUDGET_FAST + 1, complexity=0.1
    )
    assert not should_route_fast(requires_tools=False, context_tokens=100, complexity=0.5)
    # Exactly at the token budget boundary is still eligible (<=).
    assert should_route_fast(
        requires_tools=False, context_tokens=CONTEXT_TOKEN_BUDGET_FAST, complexity=0.1
    )


async def test_existing_caller_with_no_hints_never_routes_fast_even_when_bound() -> None:
    """orchestrator.py's call shape: complete(system_prompt, messages) with NO hint
    kwargs. Must behave exactly as before fast routing existed, even though a fast
    provider IS bound here — proving the defaults are fast-routing-safe."""
    primary = EchoModel(name="primary")
    fallback = EchoModel(name="fallback")
    fast = EchoModel(name="fast")
    router = LLMRouter(primary=primary, fallback=fallback, fast=fast)
    resp = await router.complete("system", [{"role": "user", "content": "hi"}])
    assert resp.provider == "primary"
    assert resp.model_class == ModelClass.primary


async def test_routes_to_fast_when_hints_qualify_and_fast_is_bound() -> None:
    primary = EchoModel(name="primary")
    fallback = EchoModel(name="fallback")
    fast = EchoModel(name="fast")
    router = LLMRouter(primary=primary, fallback=fallback, fast=fast)
    resp = await router.complete(
        "system",
        [{"role": "user", "content": "hi"}],
        requires_tools=False,
        context_tokens=500,
        complexity=0.1,
    )
    assert resp.provider == "fast"
    assert resp.model_class == ModelClass.fast
    assert resp.used_fallback is False


async def test_fast_eligible_hints_but_no_fast_bound_uses_primary() -> None:
    primary = EchoModel(name="primary")
    fallback = EchoModel(name="fallback")
    router = LLMRouter(primary=primary, fallback=fallback)  # fast not bound
    resp = await router.complete(
        "system",
        [{"role": "user", "content": "hi"}],
        requires_tools=False,
        context_tokens=500,
        complexity=0.1,
    )
    assert resp.provider == "primary"


async def test_hints_not_qualifying_uses_primary_even_with_fast_bound() -> None:
    primary = EchoModel(name="primary")
    fallback = EchoModel(name="fallback")
    fast = EchoModel(name="fast")
    router = LLMRouter(primary=primary, fallback=fallback, fast=fast)
    resp = await router.complete(
        "system",
        [{"role": "user", "content": "hi"}],
        requires_tools=True,  # disqualifies fast routing
        context_tokens=100,
        complexity=0.1,
    )
    assert resp.provider == "primary"


async def test_fast_provider_error_falls_through_to_primary_not_treated_as_failover() -> None:
    """A fast-provider failure is not the primary/fallback doctrine (§3.4.3 governs
    primary->fallback only) — it must fall through to the normal primary path."""
    primary = EchoModel(name="primary")
    fallback = EchoModel(name="fallback")
    fast = EchoModel(name="fast", raise_provider_error=True)
    router = LLMRouter(primary=primary, fallback=fallback, fast=fast)
    resp = await router.complete(
        "system",
        [{"role": "user", "content": "hi"}],
        requires_tools=False,
        context_tokens=100,
        complexity=0.1,
    )
    assert resp.provider == "primary"
    assert resp.used_fallback is False


async def test_primary_fallback_doctrine_unaffected_by_fast_binding() -> None:
    """Primary failing still triggers immediate fallback with NO retry on primary,
    identically whether or not a fast provider is bound."""
    primary = EchoModel(name="primary", raise_provider_error=True)
    fallback = EchoModel(name="fallback")
    fast = EchoModel(name="fast")
    router = LLMRouter(primary=primary, fallback=fallback, fast=fast)
    resp = await router.complete("system", [{"role": "user", "content": "hi"}])
    assert resp.provider == "fallback"
    assert resp.used_fallback is True


def test_mock_provider_factory_binds_named_fast_echo() -> None:
    settings = Settings(integration_mode="mock")
    router = build_llm_router(settings)
    assert isinstance(router._fast, EchoModel)
    assert router._fast.name == "echo_fast"


def test_live_provider_factory_binds_anthropic_fast_model_id() -> None:
    secret = json.dumps(
        {"ANTHROPIC_API_KEY": "fake-anthropic-key", "OPENAI_API_KEY": "fake-openai-key"}
    )
    settings = Settings(integration_mode="live", platform_secret=secret)
    router = build_llm_router(settings)
    assert isinstance(router._fast, AnthropicProvider)
    assert router._fast._model == settings.llm_anthropic_fast_model
    # Fast model ID differs from the primary model ID (Haiku-class vs Sonnet-class).
    assert router._fast._model != router._primary._model
