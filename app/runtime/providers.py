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

import json
import uuid
from typing import Any

from app.runtime.llm_router import LLMResponse, ModelClass, ProviderError

# Tool JSON counts against the output-token budget on both providers; a tool-bearing
# call gets at least this much headroom so a serialized tool_use block cannot be
# truncated mid-call by the plain-text 1024 ceiling (audit round 3, C2).
TOOL_CALL_MAX_OUTPUT_TOKENS = 2048


def _to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Neutral turns → Anthropic Messages shape.

    The load-bearing rule (API 400s if broken): an assistant ``tool_use`` turn must be
    immediately followed by ONE user message carrying ALL matching ``tool_result``
    blocks — so consecutive neutral ``tool_result`` turns MERGE into a single user
    message here, never one message each.
    """
    out: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def _flush_results() -> None:
        if pending_results:
            out.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for m in messages:
        role = m.get("role")
        if role == "tool_result":
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": str(m.get("call_id", "")),
                    "content": str(m.get("content", "")),
                    "is_error": bool(m.get("is_error", False)),
                }
            )
            continue
        _flush_results()
        calls = m.get("tool_calls") or []
        if role == "assistant" and calls:
            blocks: list[dict[str, Any]] = []
            text = str(m.get("content", "") or "")
            if text:
                blocks.append({"type": "text", "text": text})
            for call in calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("call_id", "")),
                        "name": str(call.get("tool_id", "")),
                        "input": call.get("inputs") or {},
                    }
                )
            out.append({"role": "assistant", "content": blocks})
            continue
        out.append({"role": str(role), "content": m.get("content", "")})
    _flush_results()
    return out


def _to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Neutral turns → OpenAI Chat Completions shape.

    ``function.arguments`` is a JSON STRING on the wire; ``role:"tool"`` messages must
    directly follow the assistant message that proposed their ``tool_call_id``s —
    the neutral ordering already guarantees that adjacency.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "tool_result":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": str(m.get("call_id", "")),
                    "content": str(m.get("content", "")),
                }
            )
            continue
        calls = m.get("tool_calls") or []
        if role == "assistant" and calls:
            out.append(
                {
                    "role": "assistant",
                    "content": str(m.get("content", "") or "") or None,
                    "tool_calls": [
                        {
                            "id": str(call.get("call_id", "")),
                            "type": "function",
                            "function": {
                                "name": str(call.get("tool_id", "")),
                                "arguments": json.dumps(call.get("inputs") or {}),
                            },
                        }
                        for call in calls
                    ],
                }
            )
            continue
        out.append({"role": str(role), "content": m.get("content", "")})
    return out


def _llm_timeout_seconds() -> float:
    from app.config import get_settings  # noqa: PLC0415 — keep the provider import light

    return float(get_settings().llm_timeout_seconds)


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

            # No SDK retries and a hard deadline (F101): the router's immediate
            # primary→fallback failover is the retry policy, and a hung primary must
            # not hold a customer's turn for minutes before it kicks in.
            self._client = AsyncAnthropic(
                api_key=self._api_key, max_retries=0, timeout=_llm_timeout_seconds()
            )
        return self._client

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        from anthropic import AnthropicError  # noqa: PLC0415 — lazy/guarded import

        client = self._get_client()
        # Prompt caching on the system-prompt prefix (§7 #10): pass system as a
        # cache-control content block. Best-effort — the API ignores it gracefully
        # if caching is unavailable for the model.
        system_blocks = [
            {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
        ]
        kwargs: dict[str, Any] = {
            "model": self._model,
            # Tool JSON shares the output budget: a tool_use block serialized into a
            # 1024-token ceiling can truncate mid-call, which surfaces as a malformed
            # tool proposal. Tool-bearing calls therefore get more headroom.
            "max_tokens": (
                max(self._max_output_tokens, TOOL_CALL_MAX_OUTPUT_TOKENS)
                if tools
                else self._max_output_tokens
            ),
            "system": system_blocks,
            "messages": _to_anthropic_messages(messages),
        }
        if tools:
            kwargs["tools"] = [
                {
                    "name": t["tool_id"],
                    "description": t["description"],
                    "input_schema": t["input_schema"],
                }
                for t in tools
            ]
        try:
            resp = await client.messages.create(**kwargs)  # type: ignore[attr-defined]
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
                    {
                        "call_id": str(getattr(block, "id", "") or uuid.uuid4().hex),
                        "tool_id": getattr(block, "name", ""),
                        "inputs": getattr(block, "input", {}) or {},
                    }
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

            self._client = AsyncOpenAI(
                api_key=self._api_key, max_retries=0, timeout=_llm_timeout_seconds()
            )
        return self._client

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        from openai import OpenAIError  # noqa: PLC0415 — lazy/guarded import

        client = self._get_client()
        # OpenAI carries the system prompt as the leading "system" message.
        api_messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        api_messages.extend(_to_openai_messages(messages))
        kwargs: dict[str, Any] = {
            "model": self._model,
            # GPT-5.x models reject `max_tokens` and require `max_completion_tokens`
            # (verified live: gpt-5.4 returns unsupported_parameter for max_tokens).
            # Sending the wrong name silently breaks the FALLBACK path — the router
            # would error instead of gracefully failing over when Anthropic is down.
            "max_completion_tokens": (
                max(self._max_output_tokens, TOOL_CALL_MAX_OUTPUT_TOKENS)
                if tools
                else self._max_output_tokens
            ),
            "messages": api_messages,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["tool_id"],
                        "description": t["description"],
                        "parameters": t["input_schema"],
                    },
                }
                for t in tools
            ]
        try:
            resp = await client.chat.completions.create(**kwargs)  # type: ignore[attr-defined]
        except OpenAIError as exc:
            raise ProviderError(f"openai_error:{type(exc).__name__}") from exc

        choice = resp.choices[0]
        text = choice.message.content or ""
        tool_calls: list[dict[str, object]] = []
        for call in getattr(choice.message, "tool_calls", None) or []:
            fn = getattr(call, "function", None)
            raw_args = getattr(fn, "arguments", "") if fn is not None else ""
            try:
                # OpenAI serializes arguments as a JSON STRING; a malformed blob drops
                # the call (never a crash) — the model simply re-proposes next turn.
                inputs = json.loads(raw_args) if raw_args else {}
            except (TypeError, ValueError):
                continue
            if not isinstance(inputs, dict):
                continue
            tool_calls.append(
                {
                    "call_id": str(getattr(call, "id", "") or uuid.uuid4().hex),
                    "tool_id": getattr(fn, "name", "") if fn is not None else "",
                    "inputs": inputs,
                }
            )
        return LLMResponse(
            text=text,
            provider=self.name,
            # OpenAI is the fallback class; model_class still reports primary because
            # the response is a primary-equivalent reply (the fast/primary split is an
            # Anthropic-side routing concern, §7 #9). used_fallback is set by the router.
            model_class=ModelClass.primary,
            confidence=1.0,
            tool_calls=tool_calls,
        )
