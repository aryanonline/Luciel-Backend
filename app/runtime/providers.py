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
import re
import uuid
from typing import Any

from app.observability.metrics import emit_metric
from app.runtime.llm_router import (
    RETRYABLE_PROVIDER_STATUSES,
    LLMResponse,
    LLMUsage,
    ModelClass,
    ProviderError,
)

# Per-call token metrics (round 7 WP-6 item 5), one datum each per model call with
# dimensions Provider (vendor) + Model. These four opt into the FULL dimension set
# (the round 7 WP-8 default publishes [Environment] only) so per-model cost and the
# prompt-cache hit rate are queryable in CloudWatch, not just in Logs Insights.
TOKEN_METRICS: tuple[tuple[str, str], ...] = (
    ("LLMInputTokens", "input_tokens"),
    ("LLMOutputTokens", "output_tokens"),
    ("LLMCacheReadTokens", "cache_read_tokens"),
    ("LLMCacheWriteTokens", "cache_write_tokens"),
)


def _tokens(value: object) -> int:
    """A token count off an SDK usage object; anything that is not a real int → 0."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def anthropic_usage(resp: object, *, model: str) -> LLMUsage | None:
    """``usage`` off an Anthropic Messages response. ``input_tokens`` there is already
    the uncached remainder; the cache hit and the cache write are reported apart."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    return LLMUsage(
        provider="anthropic",
        model=model,
        input_tokens=_tokens(getattr(usage, "input_tokens", None)),
        output_tokens=_tokens(getattr(usage, "output_tokens", None)),
        cache_read_tokens=_tokens(getattr(usage, "cache_read_input_tokens", None)),
        cache_write_tokens=_tokens(getattr(usage, "cache_creation_input_tokens", None)),
    )


def openai_usage(resp: object, *, model: str) -> LLMUsage | None:
    """``usage`` off an OpenAI Chat Completions response. ``prompt_tokens`` INCLUDES
    the cached prefix (``prompt_tokens_details.cached_tokens``), so the uncached input
    is the difference; OpenAI's implicit cache bills no write."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    details = getattr(usage, "prompt_tokens_details", None)
    cached = _tokens(getattr(details, "cached_tokens", None)) if details is not None else 0
    prompt = _tokens(getattr(usage, "prompt_tokens", None))
    return LLMUsage(
        provider="openai",
        model=model,
        input_tokens=max(prompt - cached, 0),
        output_tokens=_tokens(getattr(usage, "completion_tokens", None)),
        cache_read_tokens=cached,
        cache_write_tokens=0,
    )


def emit_usage_metrics(usage: LLMUsage | None) -> None:
    """One datum per token metric for one model call. Never raises (emit_metric)."""
    if usage is None:
        return
    dimensions = {"Provider": usage.provider, "Model": usage.model}
    for name, attr in TOKEN_METRICS:
        emit_metric(
            name,
            float(getattr(usage, attr)),
            dimensions=dimensions,
            full_dimensions=True,
        )


def _provider_error(prefix: str, exc: BaseException) -> ProviderError:
    """Map an SDK exception to ``ProviderError`` (§3.4.3), carrying its HTTP status so
    the router can tell a 429/529 refusal (worth ONE retry on the fallback leg, round 7
    WP-2 item 6) from everything else. The exception type name is safe to surface (it
    is ``kind``, which the router's ``llm_failover`` and ``llm_fallback_failed`` lines
    log); the message is not — it may embed request content."""
    raw = getattr(exc, "status_code", None)
    status = int(raw) if isinstance(raw, int) else None
    return ProviderError(
        f"{prefix}:{type(exc).__name__}",
        retryable=status in RETRYABLE_PROVIDER_STATUSES,
        status_code=status,
        kind=type(exc).__name__,
    )


# Tool JSON counts against the output-token budget on both providers; a tool-bearing
# call gets at least this much headroom so a serialized tool_use block cannot be
# truncated mid-call by the plain-text 1024 ceiling (audit round 3, C2).
TOOL_CALL_MAX_OUTPUT_TOKENS = 2048


def _output_ceiling(default: int, override: int | None, tools: list[dict[str, Any]] | None) -> int:
    """The call's output-token ceiling: the per-call ``override`` (round 7 walk run 8 —
    the graph extractor's JSON outgrew the 1024 default) or the provider's default,
    and never below ``TOOL_CALL_MAX_OUTPUT_TOKENS`` on a tool-bearing call."""
    ceiling = override if override is not None else default
    return max(ceiling, TOOL_CALL_MAX_OUTPUT_TOKENS) if tools else ceiling


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

    @property
    def model_name(self) -> str:
        """The model pin this provider calls (§7 #11), named by ``llm_failover``."""
        return self._model

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
        max_output_tokens: int | None = None,
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
            "max_tokens": _output_ceiling(self._max_output_tokens, max_output_tokens, tools),
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
            raise _provider_error("anthropic_error", exc) from exc

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
        text, confidence = parse_confidence_tag("".join(text_parts))
        usage = anthropic_usage(resp, model=self._model)
        emit_usage_metrics(usage)
        return LLMResponse(
            text=text,
            provider=self.name,
            model_class=ModelClass.primary,
            confidence=confidence,
            tool_calls=tool_calls,
            usage=usage,
            # The answer stopped at the output ceiling, mid-sentence or mid-JSON.
            truncated=getattr(resp, "stop_reason", None) == "max_tokens",
        )


# Round 6 WP-G (F124): the prompt asks the model for a trailing ``<confidence>0.0-1.0
# </confidence>`` line (input_safety.compose_confidence_stanza). It is parsed here, ONCE,
# for both providers, stripped before the text reaches the customer, and reported as
# LLMResponse.confidence. A reply without the tag keeps the pre-WP-G value 1.0, so the
# grounding-OR-confidence gate is byte-identical for models that ignore the instruction.
_CONFIDENCE_TAG = re.compile(
    r"\s*<confidence>\s*(?P<value>\d+(?:\.\d+)?|\.\d+)\s*</confidence>\s*$", re.IGNORECASE
)


def parse_confidence_tag(text: str) -> tuple[str, float]:
    """Split a trailing confidence tag off ``text`` → (clean_text, confidence in [0, 1]).

    Absent → (text unchanged, 1.0); an out-of-range number is clamped. Only a TRAILING tag
    counts: a tag quoted mid-answer is customer-visible text and is left alone."""
    m = _CONFIDENCE_TAG.search(text or "")
    if not m:
        return text, 1.0
    try:
        value = float(m.group("value"))
    except ValueError:
        return text, 1.0
    return text[: m.start()].rstrip(), max(0.0, min(1.0, value))


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

    @property
    def model_name(self) -> str:
        """The model pin this provider calls (§7 #11), named by ``llm_fallback_failed``."""
        return self._model

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
        max_output_tokens: int | None = None,
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
            "max_completion_tokens": _output_ceiling(
                self._max_output_tokens, max_output_tokens, tools
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
            raise _provider_error("openai_error", exc) from exc

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
        text, confidence = parse_confidence_tag(text)
        usage = openai_usage(resp, model=self._model)
        emit_usage_metrics(usage)
        return LLMResponse(
            text=text,
            provider=self.name,
            # OpenAI is the fallback class; model_class still reports primary because
            # the response is a primary-equivalent reply (the fast/primary split is an
            # Anthropic-side routing concern, §7 #9). used_fallback is set by the router.
            model_class=ModelClass.primary,
            confidence=confidence,
            tool_calls=tool_calls,
            usage=usage,
            truncated=getattr(choice, "finish_reason", None) == "length",
        )
