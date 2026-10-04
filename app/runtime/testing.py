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

import uuid
from typing import Any

from app.runtime.llm_router import LLMResponse, ModelClass, ProviderError


class EchoModel:
    """A deterministic LLMProvider. Echoes the last user message as the answer.

    Tool-call modeling (audit round 3, C2/C4):

      * ``tool_calls=`` (legacy ctor arg) proposes the given calls on the FIRST
        ``complete()`` only. The loop's live termination rule is "stop when a
        response proposes no tool calls" — an every-call proposer would spin every
        mock tool test to MAX_ITERATIONS, so propose-once is the deterministic
        equivalent of a model that calls its tool, reads the result, and answers.
      * ``tool_call_script=`` scripts multi-step tool turns: each ``complete()``
        pops the next list of calls (then empty). Lets a test model
        call → result → follow-up call → answer sequences deterministically.

    Every proposed call is normalized to carry a ``call_id`` (synthesized when the
    test didn't supply one), matching the provider contract.
    """

    def __init__(
        self,
        *,
        name: str = "echo",
        confidence: float = 0.9,
        tool_calls: list[dict[str, object]] | None = None,
        tool_call_script: list[list[dict[str, object]]] | None = None,
        raise_provider_error: bool = False,
        fixed_reply: str | None = None,
    ) -> None:
        self.name = name
        self._confidence = confidence
        self._raise = raise_provider_error
        self._fixed_reply = fixed_reply
        script = list(tool_call_script) if tool_call_script is not None else []
        if tool_calls:
            # Legacy arg = a one-entry script (propose once, then answer).
            script.insert(0, list(tool_calls))
        self._script = script

    @staticmethod
    def _with_call_ids(calls: list[dict[str, object]]) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for call in calls:
            normalized = dict(call)
            normalized.setdefault("call_id", f"echo_{uuid.uuid4().hex[:8]}")
            out.append(normalized)
        return out

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        if self._raise:
            # Exercises immediate failover to the fallback provider (§3.4.3).
            raise ProviderError(f"{self.name}_unavailable")
        last_user = ""
        for m in reversed(messages):
            # A platform-composed note (the knowledge-search status, round 7 WP-2
            # item 4) is not the customer's message: the echo stays on THEIR words.
            if m.get("role") == "user" and not m.get("platform_note"):
                last_user = str(m.get("content", ""))
                break
        text = self._fixed_reply if self._fixed_reply is not None else f"Echo: {last_user}"
        calls = self._with_call_ids(self._script.pop(0)) if self._script else []
        return LLMResponse(
            text=text,
            provider=self.name,
            model_class=ModelClass.primary,
            confidence=self._confidence,
            tool_calls=calls,
        )
