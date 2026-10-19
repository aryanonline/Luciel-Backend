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

import asyncio
import enum
import hashlib
import logging
import random
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

CONTEXT_TOKEN_BUDGET_FAST = 4000  # §7 #9

# Provider HTTP statuses that mean "try again in a moment", not "this request is
# wrong": 429 (rate limited) and 529 (Anthropic overloaded). Round 7 WP-2, item 6.
RETRYABLE_PROVIDER_STATUSES: frozenset[int] = frozenset({429, 529})
# The one bounded retry on the FALLBACK leg waits a short, jittered moment in this
# range (seconds), never more than a quarter of llm_timeout_seconds. Tests set (0, 0).
FALLBACK_RETRY_DELAY_RANGE: tuple[float, float] = (0.5, 1.5)


class ModelClass(str, enum.Enum):
    primary = "primary"  # Anthropic reasoning class
    fast = "fast"  # faster/cheaper variant


@dataclass
class LLMUsage:
    """The tokens ONE model call consumed, normalized across providers (round 7 WP-6
    item 5) so per-model cost is one formula on every provider.

    ``input_tokens`` is the UNCACHED prompt, billed at the model's base input rate:
    Anthropic reports it that way; OpenAI's ``prompt_tokens`` includes the cached
    part, so its provider subtracts ``cached_tokens``. ``cache_read_tokens`` is the
    prompt-cache hit (Anthropic ``cache_read_input_tokens``, OpenAI
    ``prompt_tokens_details.cached_tokens``); ``cache_write_tokens`` the cache
    creation (Anthropic ``cache_creation_input_tokens``; OpenAI caches implicitly
    and bills no write, so 0). ``provider`` is the vendor, ``model`` the pinned id."""

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


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
    # What the call cost in tokens (round 7 WP-6 item 5); None when the provider
    # reported nothing (EchoModel, an SDK response without ``usage``).
    usage: LLMUsage | None = None


def admin_id_hash(admin_id: uuid.UUID | None) -> str:
    """A stable, non-reversible tenant key for cost lines (``-`` when unbound)."""
    if admin_id is None:
        return "-"
    return hashlib.sha256(str(admin_id).encode()).hexdigest()[:16]


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
    """A provider could not serve the call.

    ``retryable`` (round 7 WP-2, item 6) is True for the 429/529 class — a refusal
    that may clear within a second — and False for everything else (auth, bad
    request, timeout, connection). Only the fallback leg ever acts on it, once."""

    def __init__(
        self, message: str = "", *, retryable: bool = False, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


def _fallback_retry_delay() -> float:
    """A short jittered wait before the ONE fallback retry, kept inside the provider
    timeout so the customer's turn cannot stretch past what one call may take."""
    low, high = FALLBACK_RETRY_DELAY_RANGE
    delay = random.uniform(low, high)  # noqa: S311  # nosec B311 — jitter, not security
    try:
        from app.config import get_settings  # noqa: PLC0415 — import cycle

        ceiling = float(get_settings().llm_timeout_seconds) / 4
    except Exception:  # noqa: BLE001 — settings unavailable → the range alone binds
        return delay
    return max(0.0, min(delay, ceiling))


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
    ingest) is admitted per call, exactly as before.

    Admission is AWAITED (round 7 WP-6 item 6: the Redis store is ``redis.asyncio``;
    the synchronous client blocked the event loop on every turn)."""

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
        # True while a ``turn()`` is open; ``_turn_paid`` once its ONE admission ran.
        self._in_turn = False
        self._turn_paid = False
        # Token usage of the open turn, per (provider, model) — logged once as the
        # ``llm_usage`` line when the turn closes (round 7 WP-6 item 5).
        self._turn_usage: dict[tuple[str, str], list[int]] = {}

    def turn(self) -> _Turn:
        """Admit the tenant's per-minute cap ONCE for one customer turn (§9 C9).

        ``async with router.turn():`` pays the admission at entry; the synchronous
        ``with router.turn():`` (the orchestrator's form) pays it in the turn's FIRST
        ``complete()``, still before any provider is called (round 7 WP-6 item 6 —
        an awaited Redis admission cannot run in a synchronous ``__enter__``). Either
        way ``LLMCapExceeded`` is raised before any model is called, so the
        orchestrator answers the turn honestly instead of an exception reaching the
        transport, and the later calls inside the block never re-admit. No admin_id
        bound (tests, legacy construction) → no cap, as before; a nested ``turn()``
        is a no-op.

        Closing the turn logs ONE ``llm_usage`` line per provider/model it used — the
        per-turn token record the round report aggregates into cost per
        conversation (item 5)."""
        return _Turn(self)

    def _open_turn(self) -> bool:
        """Open a turn; False when one is already open (a nested turn is a no-op)."""
        if self._in_turn:
            return False
        self._in_turn = True
        self._turn_paid = False
        self._turn_usage = {}
        return True

    def _close_turn(self) -> None:
        self._in_turn = False
        self._turn_paid = False
        self._log_turn_usage()

    async def _pay_turn(self) -> None:
        """The open turn's ONE admission (no-op once paid, or with no tenant bound)."""
        if self._turn_paid or self._admin_id is None:
            return
        from app.runtime.llm_cap import aadmit_llm_call  # noqa: PLC0415 — import cycle

        await aadmit_llm_call(self._admin_id)
        self._turn_paid = True

    def _note_usage(self, resp: LLMResponse) -> None:
        if not self._in_turn or resp.usage is None:
            return
        u = resp.usage
        totals = self._turn_usage.setdefault((u.provider, u.model), [0, 0, 0, 0, 0])
        for i, value in enumerate(
            (1, u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens)
        ):
            totals[i] += value

    def _log_turn_usage(self) -> None:
        usage, self._turn_usage = self._turn_usage, {}
        tenant = admin_id_hash(self._admin_id)
        for (provider, model), (calls, inp, out, cache_read, cache_write) in usage.items():
            # key=value in the message: the structured formatter renders no extras.
            logger.info(
                "llm_usage admin_id_hash=%s provider=%s model=%s calls=%d input=%d "
                "output=%d cache_read=%d cache_write=%d",
                tenant,
                provider,
                model,
                calls,
                inp,
                out,
                cache_read,
                cache_write,
            )

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
        if self._in_turn and effective_admin_id == self._admin_id:
            # Inside an open ``turn()`` the cap is paid ONCE: by the turn's first call
            # (or at ``async with`` entry) — a no-op for every later call.
            await self._pay_turn()
        elif effective_admin_id is not None:
            # Outside a turn, or a call on behalf of a DIFFERENT tenant (explicit
            # admin_id): admitted on its own. Circular import avoided: import inline.
            from app.runtime.llm_cap import aadmit_llm_call  # noqa: PLC0415

            await aadmit_llm_call(effective_admin_id)

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
                self._note_usage(resp)
                return resp
            except ProviderError:
                # Fast provider failing is NOT the primary/fallback doctrine (§3.4.3
                # governs primary→fallback only) — fall through to the normal primary
                # path below rather than treating this as a tenant-visible failure.
                pass

        try:
            resp = await self._primary.complete(system_prompt, messages, tools=tools)
            resp.used_fallback = False
            self._note_usage(resp)
            return resp
        except ProviderError:
            # Immediate failover, NO retry on primary (§3.4.3). The neutral message
            # shape replays any tool turns correctly through the other provider.
            try:
                resp = await self._fallback.complete(system_prompt, messages, tools=tools)
            except ProviderError as exc:
                if not exc.retryable:
                    raise
                # ONE bounded retry on the fallback leg for the 429/529 class only
                # (round 7 WP-2, item 6): a 529 on the primary and a 429 on the
                # fallback in the same second used to declare both providers down
                # and hand the customer the outage line. Anything else — auth, a bad
                # request, a timeout — still fails immediately.
                await asyncio.sleep(_fallback_retry_delay())
                resp = await self._fallback.complete(system_prompt, messages, tools=tools)
            resp.used_fallback = True
            self._note_usage(resp)
            return resp


class _Turn:
    """``router.turn()``: a sync AND async context manager over one customer turn.

    ``with`` (the orchestrator's form) opens the turn and leaves the cap admission to
    the turn's first ``complete()``; ``async with`` awaits it at entry. Exiting
    closes the turn and logs its ``llm_usage`` line, on success or exception."""

    __slots__ = ("_opened", "_router")

    def __init__(self, router: LLMRouter) -> None:
        self._router = router
        self._opened = False

    def __enter__(self) -> None:
        self._opened = self._router._open_turn()

    def __exit__(self, *exc: object) -> None:
        if self._opened:
            self._opened = False
            self._router._close_turn()

    async def __aenter__(self) -> None:
        self._opened = self._router._open_turn()
        if not self._opened:
            return
        try:
            await self._router._pay_turn()
        except BaseException:
            # An exception in __aenter__ skips __aexit__: close the turn here.
            self._opened = False
            self._router._close_turn()
            raise

    async def __aexit__(self, *exc: object) -> None:
        self.__exit__(*exc)
