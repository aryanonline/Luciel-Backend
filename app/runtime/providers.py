"""Real Anthropic / OpenAI LLM providers (DOCTRINE ANCHOR — Architecture §3.4.3).

These bind the production LLM clients behind the same ``LLMProvider`` Protocol that
``EchoModel`` implements, so the router, the agentic loop, and the per-tenant cap are
unchanged by the cutover (§3.4.3). They are only constructed in ``live`` mode by
``app.runtime.provider_factory``; in the default ``mock`` mode nothing here runs.

Doctrine bindings honored here:
  - Anthropic is primary, OpenAI is the fallback (§3.4.3 / §7 #7/#8). The router owns
    failover; each provider just maps its own SDK errors to ``ProviderError`` so the
    router's immediate, no-retry failover triggers correctly.
  - Model IDs are platform-controlled and centralized in ``app.config`` (§7 #11) — no
    magic model strings live here.
  - Prompt caching is applied to the system-prompt prefix where the SDK supports it
    (§7 #10) — best-effort; a non-caching SDK path still works.

Both SDKs are imported LAZILY inside methods so CI (no keys, no network, SDK may be
absent) imports this module cleanly. No network call happens at import or construction
time — clients are built on first ``complete``.
"""

from __future__ import annotations

from app.runtime.llm_router import LLMResponse, ModelClass, ProviderError


class AnthropicProvider:
    """Primary provider — Anthropic Messages API behind the LLMProvider Protocol.

    Maps ``anthropic.AnthropicError`` (the SDK's base exception) to ``ProviderError``
    so a primary failure triggers the router's immediate failover to OpenAI (§3.4.3)."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_output_tokens: int = 1024,
        name: str = "anthropic",
    ) -> None:
        self.name = name
        self._api_key = api_key
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._client: object | None = None

    def _get_client(self) -> object:
        """Lazily construct the async SDK client (no network at construction)."""
        if self._client is None:
            from anthropic import AsyncAnthropic  # noqa: PLC0415 — lazy/guarded import

            self._client = AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def complete(self, system_prompt: str, messages: list[dict[str, str]]) -> LLMResponse:
        from anthropic import AnthropicError  # noqa: PLC0415 — lazy/guarded import

        client = self._get_client()
        # Prompt caching on the system-prompt prefix (§7 #10): pass system as a
        # cache-control content block. Best-effort — the API ignores it gracefully
        # if caching is unavailable for the model.
        system_blocks = [
            {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
        ]
        api_messages = [{"role": m["role"], "content": m["content"]} for m in messages]
        try:
            resp = await client.messages.create(  # type: ignore[attr-defined]
                model=self._model,
                max_tokens=self._max_output_tokens,
                system=system_blocks,
                messages=api_messages,
            )
        except AnthropicError as exc:
            # Map ANY SDK error to ProviderError so the router fails over (§3.4.3).
            # The exception type name is safe to surface; the message is not logged
            # here to avoid leaking request content.
            raise ProviderError(f"anthropic_error:{type(exc).__name__}") from exc

        text_parts: list[str] = []
        tool_calls: list[dict[str, object]] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", ""))
            elif btype == "tool_use":
                tool_calls.append(
                    {"tool_id": getattr(block, "name", ""), "inputs": getattr(block, "input", {})}
                )
        return LLMResponse(
            text="".join(text_parts),
            provider=self.name,
            model_class=ModelClass.primary,
            confidence=1.0,
            tool_calls=tool_calls,
        )


class OpenAIProvider:
    """Fallback provider — OpenAI Chat Completions behind the LLMProvider Protocol.

    Maps ``openai.OpenAIError`` (the SDK's base exception) to ``ProviderError`` so the
    router treats an OpenAI failure uniformly (§3.4.3)."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_output_tokens: int = 1024,
        name: str = "openai",
    ) -> None:
        self.name = name
        self._api_key = api_key
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._client: object | None = None

    def _get_client(self) -> object:
        if self._client is None:
            from openai import AsyncOpenAI  # noqa: PLC0415 — lazy/guarded import

            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def complete(self, system_prompt: str, messages: list[dict[str, str]]) -> LLMResponse:
        from openai import OpenAIError  # noqa: PLC0415 — lazy/guarded import

        client = self._get_client()
        # OpenAI carries the system prompt as the leading "system" message.
        api_messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        api_messages.extend({"role": m["role"], "content": m["content"]} for m in messages)
        try:
            resp = await client.chat.completions.create(  # type: ignore[attr-defined]
                model=self._model,
                max_tokens=self._max_output_tokens,
                messages=api_messages,
            )
        except OpenAIError as exc:
            raise ProviderError(f"openai_error:{type(exc).__name__}") from exc

        choice = resp.choices[0]
        text = choice.message.content or ""
        return LLMResponse(
            text=text,
            provider=self.name,
            # OpenAI is the fallback class; model_class still reports primary because
            # the response is a primary-equivalent reply (the fast/primary split is an
            # Anthropic-side routing concern, §7 #9). used_fallback is set by the router.
            model_class=ModelClass.primary,
            confidence=1.0,
            tool_calls=[],
        )
