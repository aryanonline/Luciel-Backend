"""Deterministic LLM provider for the loop + tests (Architecture §3.4.3).

EchoModel implements the LLMProvider protocol without any external API, so the
agentic loop, the router failover, and the contract tests run hermetically. The
real Anthropic/OpenAI clients bind behind the same interface in deployment.

EchoModel is intentionally simple and PREDICTABLE: it echoes the latest user
message, reports a configurable confidence, and can be told to propose tool calls
or to raise ProviderError (to exercise router failover, §3.4.3). It never invents
content beyond the input, which keeps grounding/faithful-readback tests meaningful.
"""

from __future__ import annotations

from app.runtime.llm_router import LLMResponse, ModelClass, ProviderError


class EchoModel:
    """A deterministic LLMProvider. Echoes the last user message as the answer."""

    def __init__(
        self,
        *,
        name: str = "echo",
        confidence: float = 0.9,
        tool_calls: list[dict[str, object]] | None = None,
        raise_provider_error: bool = False,
        fixed_reply: str | None = None,
    ) -> None:
        self.name = name
        self._confidence = confidence
        self._tool_calls = tool_calls or []
        self._raise = raise_provider_error
        self._fixed_reply = fixed_reply

    async def complete(self, system_prompt: str, messages: list[dict[str, str]]) -> LLMResponse:
        if self._raise:
            # Exercises immediate failover to the fallback provider (§3.4.3).
            raise ProviderError(f"{self.name}_unavailable")
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = m.get("content", "")
                break
        text = self._fixed_reply if self._fixed_reply is not None else f"Echo: {last_user}"
        return LLMResponse(
            text=text,
            provider=self.name,
            model_class=ModelClass.primary,
            confidence=self._confidence,
            tool_calls=list(self._tool_calls),
        )
