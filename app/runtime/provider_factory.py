"""Mode-aware LLM router construction (GATE 2 cutover — Architecture §3.4.3).

The single wiring point that decides, from ``integration_mode``, whether the runtime
talks to the deterministic ``EchoModel`` (default ``mock``) or the real Anthropic /
OpenAI providers (``live``). Callers (the widget data plane, future runtime paths)
depend only on the returned ``LLMRouter`` — they never branch on mode themselves.

RUNTIME-UNCHANGED GUARANTEE: with ``integration_mode`` unset or ``mock`` this returns
exactly the pre-cutover wiring — ``LLMRouter(EchoModel, EchoModel)`` — so no external
key is read and no network call is possible. Only ``live`` reads the platform secret.
"""

from __future__ import annotations

import uuid

from app.config import Settings, get_settings, parse_platform_secret
from app.runtime.llm_router import LLMRouter


def build_llm_router(
    settings: Settings | None = None,
    *,
    admin_id: uuid.UUID | None = None,
) -> LLMRouter:
    """Build the LLMRouter for the active integration mode.

    - ``mock`` (default): ``LLMRouter(EchoModel, EchoModel)`` — identical to the
      pre-cutover wiring; no keys, no network.
    - ``live``: Anthropic primary + OpenAI fallback, keys parsed from the platform
      secret, model IDs from config (§7 #11). ``admin_id`` flows to the router so the
      per-tenant LLM cap applies (§9 C9)."""
    settings = settings if settings is not None else get_settings()

    if settings.integration_mode == "live":
        return _build_live_router(settings, admin_id=admin_id)

    # Default / mock — exactly as before the cutover.
    from app.runtime.testing import EchoModel  # noqa: PLC0415 — keep import local

    echo = EchoModel(confidence=0.9)
    return LLMRouter(primary=echo, fallback=echo, admin_id=admin_id)


def _build_live_router(settings: Settings, *, admin_id: uuid.UUID | None) -> LLMRouter:
    # Imported lazily so the mock path never imports the SDK-backed providers.
    from app.runtime.providers import AnthropicProvider, OpenAIProvider  # noqa: PLC0415

    secret = parse_platform_secret(settings.platform_secret)
    anthropic_key = secret.get("ANTHROPIC_API_KEY", "")
    openai_key = secret.get("OPENAI_API_KEY", "")
    if not anthropic_key or not openai_key:
        # Fail loud at construction (never silently fall back to mock in live mode):
        # a live deploy with missing keys is a misconfiguration, not a degrade case.
        # The message names which keys are missing but NEVER echoes any value.
        missing = [
            n
            for n, v in (("ANTHROPIC_API_KEY", anthropic_key), ("OPENAI_API_KEY", openai_key))
            if not v
        ]
        raise RuntimeError(
            f"integration_mode=live but platform secret is missing keys: {', '.join(missing)}"
        )

    primary = AnthropicProvider(
        api_key=anthropic_key,
        model=settings.llm_anthropic_primary_model,
        max_output_tokens=settings.llm_max_output_tokens,
    )
    fallback = OpenAIProvider(
        api_key=openai_key,
        model=settings.llm_openai_fallback_model,
        max_output_tokens=settings.llm_max_output_tokens,
    )
    return LLMRouter(primary=primary, fallback=fallback, admin_id=admin_id)
