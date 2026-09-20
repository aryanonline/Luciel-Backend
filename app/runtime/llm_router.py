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

import contextlib
import enum
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

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
    # Tool-call intents the model proposes (PLAN phase). Each:
    # {call_id, tool_id, inputs} — call_id is the provider's tool-use id (Anthropic
    # block id / OpenAI tool_call id; synthesized for EchoModel) and MUST be echoed
    # back on the matching tool_result turn, or the provider APIs reject the thread.
    tool_calls: list[dict[str, object]] = field(default_factory=list)


class LLMProvider(Protocol):
    """One LLM behind a neutral, provider-agnostic completion surface.

    ``messages`` carries three neutral turn shapes (audit round 3, C2), which each
    provider maps to its native wire format:

      {"role": "user"|"assistant", "content": str}                    — plain turn
      {"role": "assistant", "content": str,
       "tool_calls": [{"call_id", "tool_id", "inputs"}]}              — model proposed tools
      {"role": "tool_result", "call_id": str, "tool_id": str,
       "content": str, "is_error": bool}                              — broker outcome fed back

    ``tools`` is the provider-neutral tool-def list (app/tools/schemas.build_tool_defs);
    None keeps the call byte-identical to the pre-tool-use behavior. The neutral shape
    is what makes failover safe: a thread begun on Anthropic replays correctly through
    OpenAI because the router stores only neutral turns, never native ones.
    """

    name: str

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse: ...


class ProviderError(Exception):
    pass


def should_route_fast(*, requires_tools: bool, context_tokens: int, complexity: float) -> bool:
    """Intra-session fast/cheap routing gate (§3.4.3 / §7 #9) — all three conditions."""
    return not requires_tools and context_tokens <= CONTEXT_TOKEN_BUDGET_FAST and complexity < 0.5


class LLMRouter:
    """Routes to primary, falls back to secondary immediately on primary failure
    (no retry on primary). Fallback is recorded but never surfaced.

    Architecture §9 C9 / §4.6: enforces per-admin_id concurrency cap (50/min).
    Raises ``LLMCapExceeded`` when the tenant is over cap. The cap is checked at
    the router level so it is uniform across all callers (orchestrator, ingest,
    any future runtime path).

    The unit the cap counts is the customer TURN, not the model call (round 7 WP-2,
    item 4): the orchestrator opens ``turn()`` once per inbound message, and every
    ``complete()`` inside it skips re-admission — a tool-using turn used to burn up
    to five of the fifty. A caller that never opens a turn (graph extraction at
    ingest) is admitted per call, exactly as before."""

    def __init__(
        self,
        primary: LLMProvider,
        fallback: LLMProvider,
        admin_id: uuid.UUID | None = None,
        fast: LLMProvider | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        # admin_id is used for the per-tenant LLM concurrency cap (§9 C9).
        # When None (legacy or test construction), the cap is skipped.
        self._admin_id = admin_id
        # Optional Haiku-class fast/cheap provider (§3.4.3 / §7 #9). When unbound
        # (None), routing is 100% unchanged from pre-fast-routing behavior — the
        # gate in .complete() below can never select fast without a bound provider.
        self._fast = fast
        # True while a ``turn()`` is open: the cap was paid at the turn boundary.
        self._in_turn = False

    @contextlib.contextmanager
    def turn(self) -> Iterator[None]:
        """Admit the tenant's per-minute cap ONCE for one customer turn (§9 C9).

        Raises ``LLMCapExceeded`` at entry when the tenant is over cap — before any
        model is called — so the orchestrator answers the turn honestly instead of
        an exception reaching the transport. Inside the block ``complete()`` never
        re-admits. No admin_id bound (tests, legacy construction) → no cap, as
        before; a nested ``turn()`` is a no-op."""
        if self._in_turn or self._admin_id is None:
            yield
            return
        from app.runtime.llm_cap import admit_llm_call  # noqa: PLC0415 — import cycle

        admit_llm_call(self._admin_id)
        self._in_turn = True
        try:
            yield
        finally:
            self._in_turn = False

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        *,
        admin_id: uuid.UUID | None = None,
        requires_tools: bool = True,
        context_tokens: int = CONTEXT_TOKEN_BUDGET_FAST + 1,
        complexity: float = 1.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """Complete with per-admin_id LLM concurrency cap enforcement (§9 C9).

        ``admin_id`` kwarg overrides the instance-level admin_id; both are optional
        for backwards compatibility (existing callers that don't pass admin_id skip
        the cap — they should be updated to pass admin_id).

        ``requires_tools`` / ``context_tokens`` / ``complexity`` are routing hints for
        the intra-session fast/cheap gate (§3.4.3 / §7 #9). Their defaults are
        deliberately chosen so that a caller who omits them (e.g. orchestrator.py's
        existing ``self.router.complete(self.system_prompt, messages)`` call site)
        NEVER triggers fast routing — behavior for existing callers is unchanged
        byte-for-byte. Only a caller that explicitly passes fast-eligible hints, AND
        only when a fast provider is bound (``fast=`` at construction), can route to
        the fast model. Anthropic-primary/OpenAI-fallback doctrine and the NO-retry-
        on-primary failover behavior are unaffected either way.
        """
        effective_admin_id = admin_id if admin_id is not None else self._admin_id
        # Inside an open ``turn()`` the cap was paid at the turn boundary; only a call
        # on behalf of a DIFFERENT tenant (explicit admin_id) is admitted on its own.
        already_paid = self._in_turn and effective_admin_id == self._admin_id
        if effective_admin_id is not None and not already_paid:
            # Circular import avoided: import inline, module is small + stable.
            from app.runtime.llm_cap import admit_llm_call  # noqa: PLC0415

            admit_llm_call(effective_admin_id)

        use_fast = self._fast is not None and should_route_fast(
            requires_tools=requires_tools,
            context_tokens=context_tokens,
            complexity=complexity,
        )
        if use_fast:
            try:
                resp = await self._fast.complete(  # type: ignore[union-attr]
                    system_prompt, messages, tools=tools
                )
                resp.model_class = ModelClass.fast
                resp.used_fallback = False
                return resp
            except ProviderError:
                # Fast provider failing is NOT the primary/fallback doctrine (§3.4.3
                # governs primary→fallback only) — fall through to the normal primary
                # path below rather than treating this as a tenant-visible failure.
                pass

        try:
            resp = await self._primary.complete(system_prompt, messages, tools=tools)
            resp.used_fallback = False
            return resp
        except ProviderError:
            # Immediate failover, NO retry on primary (§3.4.3). The neutral message
            # shape replays any tool turns correctly through the other provider.
            resp = await self._fallback.complete(system_prompt, messages, tools=tools)
            resp.used_fallback = True
            return resp
