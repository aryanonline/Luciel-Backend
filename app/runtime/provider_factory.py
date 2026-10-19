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
from typing import TYPE_CHECKING

from app.config import Settings, get_settings, parse_platform_secret
from app.runtime.llm_router import LLMRouter

if TYPE_CHECKING:
    from app.runtime.speech import TextToSpeech


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
    # A distinctly-named fast Echo double lets fast-routing (§3.4.3 / §7 #9) be
    # exercised end-to-end in mock mode too (tests can assert model_class/provider
    # switched) without adding any new class to app.runtime.testing (out of scope
    # for this workstream) or opening any network path.
    echo_fast = EchoModel(name="echo_fast", confidence=0.9)
    return LLMRouter(primary=echo, fallback=echo, admin_id=admin_id, fast=echo_fast)


def _build_live_router(settings: Settings, *, admin_id: uuid.UUID | None) -> LLMRouter:
    # Imported lazily so the mock path never imports the SDK-backed providers.
    from app.runtime.providers import AnthropicProvider, OpenAIProvider  # noqa: PLC0415

    secret = parse_platform_secret(settings.platform_secret, live=True)
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
    # Haiku-class fast provider (§3.4.3 / §7 #9) — reuses AnthropicProvider (a fast
    # model is still an Anthropic model, just a cheaper/smaller one) with the fast
    # model ID already present in config. Keeps the Anthropic-primary/OpenAI-fallback
    # doctrine intact: fast is an ADDITIONAL routing tier the primary/fallback pair
    # sits behind, never a replacement for either leg of that doctrine.
    fast = AnthropicProvider(
        api_key=anthropic_key,
        model=settings.llm_anthropic_fast_model,
        max_output_tokens=settings.llm_max_output_tokens,
        name="anthropic_fast",
    )
    return LLMRouter(primary=primary, fallback=fallback, admin_id=admin_id, fast=fast)


# --- text-to-speech provider factory (§3.1.2 / §5.12) -------------------------- #
# Mirrors build_llm_router's mode-aware wiring exactly: mock mode returns the
# deterministic Echo double (no keys, no network), live mode reads ELEVENLABS_API_KEY
# from the platform secret — never hardcoded (Hard rule #2) — and the voice/model ids
# from Settings (round 7 WP-6 item 9). There is no speech-to-text factory: live STT is
# Twilio's own <Gather input="speech"> transcript, so the Whisper client this used to
# construct (on OPENAI_API_KEY, pinned to whisper-1) was never called by anything.


def build_text_to_speech(settings: Settings | None = None) -> TextToSpeech:
    """Build the TTS provider for the active integration mode.

    - ``mock`` (default): ``EchoTextToSpeech()`` — no keys, no network.
    - ``live``: ``ElevenLabsTextToSpeech`` with the platform ``ELEVENLABS_API_KEY``
      and the platform-controlled ``elevenlabs_voice_id`` / ``elevenlabs_model_id``
      (§5.12 sub-processor register: ElevenLabs TTS). A missing key fails loud.
    """
    settings = settings if settings is not None else get_settings()

    if settings.integration_mode != "live":
        from app.runtime.speech import EchoTextToSpeech  # noqa: PLC0415

        return EchoTextToSpeech()

    from app.runtime.speech import ElevenLabsTextToSpeech  # noqa: PLC0415

    elevenlabs_key = parse_platform_secret(settings.platform_secret, live=True).get(
        "ELEVENLABS_API_KEY", ""
    )
    if not elevenlabs_key:
        # Fail loud at construction, same doctrine as _build_live_router: name the
        # missing key, never echo any value.
        raise RuntimeError(
            "integration_mode=live but platform secret is missing keys: ELEVENLABS_API_KEY"
        )
    return ElevenLabsTextToSpeech(
        api_key=elevenlabs_key,
        voice_id=settings.elevenlabs_voice_id,
        model_id=settings.elevenlabs_model_id,
    )
