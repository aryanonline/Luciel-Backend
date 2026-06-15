"""LLM provider + model routing (DOCTRINE ANCHOR — Architecture §3.4.3).

Anthropic primary, OpenAI fallback with IMMEDIATE failover and NO retry on primary
(§3.4.3 / Locked Decision §7 #7/#8). Intra-session fast/cheap routing when all three
hold: (a) no tools required, (b) retrieved context <= 4K tokens, (c) complexity
below threshold (§7 #9). Model selection is platform-controlled, never an admin
field (§7 #11). Provider fallback is logged in the trace, never surfaced to admins
or end customers. Prompt caching on the system-prompt prefix (§7 #10).

The provider clients are abstracted behind LLMProvider; a deterministic EchoModel
exercises the router + loop without external APIs. Real Anthropic/OpenAI clients
bind in deployment behind the same interface.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Protocol

CONTEXT_TOKEN_BUDGET_FAST = 4000  # §7 #9


class ModelClass(str, enum.Enum):
    primary = "primary"  # Anthropic reasoning class
    fast = "fast"  # faster/cheaper variant


@dataclass
class LLMResponse:
    text: str
    provider: str
    model_class: ModelClass
    confidence: float
    used_fallback: bool = False
    # Tool-call intents the model proposes (PLAN phase). Each: {tool_id, inputs}.
    tool_calls: list[dict[str, object]] = field(default_factory=list)


class LLMProvider(Protocol):
    name: str

    async def complete(self, system_prompt: str, messages: list[dict[str, str]]) -> LLMResponse: ...


class ProviderError(Exception):
    pass


def should_route_fast(*, requires_tools: bool, context_tokens: int, complexity: float) -> bool:
    """Intra-session fast/cheap routing gate (§3.4.3 / §7 #9) — all three conditions."""
    return not requires_tools and context_tokens <= CONTEXT_TOKEN_BUDGET_FAST and complexity < 0.5


class LLMRouter:
    """Routes to primary, falls back to secondary immediately on primary failure
    (no retry on primary). Fallback is recorded but never surfaced."""

    def __init__(self, primary: LLMProvider, fallback: LLMProvider) -> None:
        self._primary = primary
        self._fallback = fallback

    async def complete(self, system_prompt: str, messages: list[dict[str, str]]) -> LLMResponse:
        try:
            resp = await self._primary.complete(system_prompt, messages)
            resp.used_fallback = False
            return resp
        except ProviderError:
            # Immediate failover, NO retry on primary (§3.4.3).
            resp = await self._fallback.complete(system_prompt, messages)
            resp.used_fallback = True
            return resp
