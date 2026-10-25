"""Per-admin_id LLM concurrency rate cap (Architecture §9 Commitment 9 / §4.6).

Architecture §9 C9 / §4.6: "Each tenant has a per-minute LLM call concurrency cap
(default: 50 concurrent LLM calls/minute per admin_id). This prevents a single
tenant with a spike from monopolizing Anthropic/OpenAI throughput at the platform's
API keys."

Implementation mirrors ``app/channels/rate_limit.py`` (in-memory; Redis impl
mirrors the same interface). Keyed on admin_id. Uses a sliding 60-second window
count (not a token bucket, since LLM calls are not refillable mid-window — we want
a strict per-minute cap).

The real production backend is Redis-backed (INCR + EXPIRE on a 60-second key);
the in-memory fallback here is exercisable without Redis and is the test/CI impl.

Round 7 WP-6 item 6: the Redis store talks ``redis.asyncio`` on the request path
(the synchronous client blocked the event loop for every turn's admission), and it
never latches the in-memory store: while Redis is unreachable it counts in-process
and tries to bind again every ``REDIS_REBIND_INTERVAL_SECONDS``.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# Default: 50 LLM calls per admin_id per 60-second window (Architecture §9 C9).
LLM_CAP_PER_MINUTE: int = 50
LLM_CAP_WINDOW_SECONDS: float = 60.0
# While the Redis store is unbound (the bind-time ping failed, or a command failed)
# it counts in-process and tries to bind again at most this often (item 6).
REDIS_REBIND_INTERVAL_SECONDS: float = 60.0
# Admission sits on the customer's turn: a Redis that does not answer must not hold
# it. Connect and command budget for one admission, then the in-process count.
REDIS_SOCKET_TIMEOUT_SECONDS: float = 0.5


class LLMCapExceeded(Exception):
    """Raised when a tenant's per-minute LLM call cap is exceeded."""

    def __init__(self, admin_id: uuid.UUID) -> None:
        self.admin_id = admin_id
        super().__init__(f"LLM concurrency cap exceeded for admin_id={admin_id}")


class InMemoryLLMCapStore:
    """Sliding-window LLM call counter (test/CI). Redis impl mirrors this API.

    Pattern mirrors ``rate_limit.TokenBucketStore._tenant_minute`` but uses a
    true sliding-window list rather than a single-bucket count, for exactness.
    """

    def __init__(self) -> None:
        # Maps admin_id -> sorted list of call timestamps within the window.
        self._calls: dict[str, list[float]] = {}

    def _now(self) -> float:
        return time.monotonic()

    def admit(self, admin_id: str, *, cap: int = LLM_CAP_PER_MINUTE) -> bool:
        """Attempt to admit one LLM call for admin_id. Returns True if admitted."""
        now = self._now()
        window_start = now - LLM_CAP_WINDOW_SECONDS
        calls = self._calls.get(admin_id, [])
        # Evict timestamps outside the sliding window.
        calls = [ts for ts in calls if ts >= window_start]
        if len(calls) >= cap:
            self._calls[admin_id] = calls
            return False
        calls.append(now)
        self._calls[admin_id] = calls
        return True

    def current_count(self, admin_id: str) -> int:
        """Number of LLM calls in the current 60-second window (observability)."""
        now = self._now()
        window_start = now - LLM_CAP_WINDOW_SECONDS
        calls = self._calls.get(admin_id, [])
        return sum(1 for ts in calls if ts >= window_start)

    async def aadmit(self, admin_id: str, *, cap: int = LLM_CAP_PER_MINUTE) -> bool:
        """The async admission the router awaits (same arithmetic, no I/O)."""
        return self.admit(admin_id, cap=cap)

    async def acurrent_count(self, admin_id: str) -> int:
        return self.current_count(admin_id)


async def _close_quietly(client: Any) -> None:
    if client is None:
        return
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001 — a dead client closing badly changes nothing
        logger.debug("LLM cap: closing the redis client failed", exc_info=True)


def _tenant_hash(admin_id: str) -> str:
    """A log-safe tenant handle: the same sha256[:16] of the id that llm_router's
    ``admin_id_hash`` writes on cost lines, so one tenant is one handle across logs
    (the raw id never enters a log line)."""
    return hashlib.sha256(admin_id.encode()).hexdigest()[:16]


class RedisLLMCapStore:
    """Redis-backed per-admin_id LLM cap (production backend, Architecture §9 C9).

    Mirrors ``InMemoryLLMCapStore``'s interface using a fixed 60-second key per
    admin_id: ``INCR`` the counter then ``EXPIRE`` it on first touch. This is the
    per-minute window the module docstring describes — a strict cap, not a refillable
    bucket. Redis is shared across tasks/containers so the cap holds platform-wide
    (the in-memory store only counts within one process).

    ASYNC (round 7 WP-6 item 6): the client is a ``redis.asyncio`` client and the
    router awaits ``aadmit`` — the synchronous client used to block the event loop
    (every other request on the task) for each admission's round-trip.

    GRACEFUL DEGRADATION (cost-control doctrine, §9 C9 / §4.6): the cap is a cost/abuse
    control, so failing OPEN (removing the cap) on a Redis outage is NOT acceptable —
    one tenant could then monopolize the platform keys. But a Redis outage must not
    drop ALL traffic either. So on any Redis error we log and fall back to a
    process-local ``InMemoryLLMCapStore`` (per-task counting). The cap stays in force,
    degraded to per-process granularity until Redis recovers.

    RE-BINDING (item 6): built with a ``client_factory`` (production), the store binds
    lazily on the first admission (a ``PING``), and while unbound — the ping failed,
    or a command failed and the client was dropped — it tries again at most every
    ``rebind_interval`` seconds. A Redis that was down when the task booted used to
    leave the in-memory store latched for the life of the task. A client injected
    directly (tests) is used as given and never re-bound."""

    def __init__(
        self,
        client: Any = None,
        *,
        client_factory: Callable[[], Any] | None = None,
        rebind_interval: float = REDIS_REBIND_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        # The loop a factory-built client was bound on: redis.asyncio connections
        # belong to one event loop (None = injected client, never checked).
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._factory = client_factory
        self._rebind_interval = rebind_interval
        self._clock = clock
        self._next_bind_at = 0.0
        # Per-process fallback used only while Redis is unreachable (degraded mode).
        self._fallback = InMemoryLLMCapStore()
        # admin_id -> (platform-wide count an INCR returned, when): the non-blocking
        # read behind the synchronous ``current_count``.
        self._observed: dict[str, tuple[int, float]] = {}

    @staticmethod
    def _key(admin_id: str) -> str:
        return f"llmcap:{admin_id}"

    @property
    def bound(self) -> bool:
        """True while a Redis client is in use (False = counting in-process)."""
        return self._client is not None

    async def _bound_client(self) -> Any:
        loop = asyncio.get_running_loop()
        if self._client is not None and self._client_loop not in (None, loop):
            # Bound on another event loop (a process that ran asyncio.run twice): its
            # connections cannot be awaited here. Not a failure — bind afresh now.
            self._client = None
            self._client_loop = None
            self._next_bind_at = 0.0
        if self._client is not None:
            return self._client
        if self._factory is None:
            return None
        now = self._clock()
        if now < self._next_bind_at:
            return None
        self._next_bind_at = now + self._rebind_interval
        client = None
        try:
            client = self._factory()
            await client.ping()
        except Exception:  # noqa: BLE001 — unreachable → in-process count, retry later
            logger.warning(
                "LLM cap: redis unreachable; counting in-process (degraded) — next bind "
                "attempt in %.0f s",
                self._rebind_interval,
                exc_info=True,
            )
            await _close_quietly(client)
            return None
        self._client = client
        self._client_loop = loop
        logger.info("LLM cap: redis bound; the per-minute cap is platform-wide again")
        return client

    async def _drop(self, client: Any) -> None:
        """Forget a client whose command failed; the next bind waits the interval so
        a dead Redis costs one timeout per interval, not one per turn."""
        if self._factory is None:
            return  # an injected client is kept; each call degrades on its own
        if self._client is client:
            self._client = None
            self._client_loop = None
            self._next_bind_at = self._clock() + self._rebind_interval
        await _close_quietly(client)

    async def aadmit(self, admin_id: str, *, cap: int = LLM_CAP_PER_MINUTE) -> bool:
        client = await self._bound_client()
        if client is None:
            return self._fallback.admit(admin_id, cap=cap)
        key = self._key(admin_id)
        try:
            # INCR and TTL in ONE round trip: the window must always carry an expiry.
            # It used to be set only when the INCR returned 1, so a key-creating INCR
            # whose reply was lost (the 0.5 s socket timeout, a stalled loop, a dropped
            # connection, a task dying before its EXPIRE) left a key that counted
            # forever: the tenant's 50th call EVER was refused, and every call after it,
            # until an operator deleted the key by hand (Phase 3 review, 2026-10-19).
            # Now every admission repairs a window without a TTL.
            async with client.pipeline(transaction=False) as pipe:
                pipe.incr(key)
                pipe.ttl(key)
                raw_count, raw_ttl = await pipe.execute()
            count = int(raw_count)
            if int(raw_ttl) < 0:  # -1: no expiry; -2: gone between the two (harmless)
                if count > 1:
                    # A heal, not a fault (and two admissions opening one window
                    # concurrently can land here with count=2 before the first EXPIRE):
                    # WARNING, never the ERROR the sweep's metric filter pages on.
                    logger.warning(
                        "LLM cap window for tenant %s had no expiry (count=%d); repaired to %ss",
                        _tenant_hash(admin_id),
                        count,
                        int(LLM_CAP_WINDOW_SECONDS),
                    )
                await client.expire(key, int(LLM_CAP_WINDOW_SECONDS))
        except Exception:  # noqa: BLE001 — any redis error → degrade, never fail open
            logger.warning(
                "LLM cap Redis unavailable; degrading to in-memory per-process counting",
                exc_info=True,
            )
            await self._drop(client)
            return self._fallback.admit(admin_id, cap=cap)
        self._observed[admin_id] = (count, self._clock())
        return count <= cap

    async def acurrent_count(self, admin_id: str) -> int:
        """The exact platform-wide count (a Redis GET)."""
        client = await self._bound_client()
        if client is None:
            return self._fallback.current_count(admin_id)
        try:
            raw = await client.get(self._key(admin_id))
            return int(raw) if raw is not None else 0
        except Exception:  # noqa: BLE001 — observability only; degrade quietly
            logger.warning("LLM cap Redis unavailable for current_count", exc_info=True)
            await self._drop(client)
            return self._fallback.current_count(admin_id)

    def admit(self, admin_id: str, *, cap: int = LLM_CAP_PER_MINUTE) -> bool:
        """SYNCHRONOUS admission cannot reach Redis without blocking the event loop,
        so it counts in this process only. The router awaits ``aadmit``; this keeps
        the store interface whole for the legacy synchronous ``admit_llm_call``."""
        return self._fallback.admit(admin_id, cap=cap)

    def current_count(self, admin_id: str) -> int:
        """A NON-BLOCKING read for synchronous callers (graph extraction's ingest
        headroom check): the platform-wide count this process's own last INCR
        returned within the window, or the in-process degraded count, whichever is
        higher. It can lag other tasks' admissions since then, and it errs high (the
        key may expire before the observation ages out) — a headroom check that errs
        toward skipping. ``acurrent_count`` is the exact read."""
        seen = self._observed.get(admin_id)
        observed = (
            seen[0] if seen is not None and self._clock() - seen[1] < LLM_CAP_WINDOW_SECONDS else 0
        )
        return max(observed, self._fallback.current_count(admin_id))


_store: InMemoryLLMCapStore | RedisLLMCapStore | None = None


def get_llm_cap_store() -> InMemoryLLMCapStore | RedisLLMCapStore:
    """Return the process-global LLM cap store.

    In ``live`` mode this is the ``RedisLLMCapStore``, which binds to Redis lazily on
    the first admission and re-binds on a bounded schedule while Redis is unreachable
    (counting in-process meanwhile — graceful degradation, see ``RedisLLMCapStore``).
    In every other mode (the default ``mock``) it stays ``InMemoryLLMCapStore`` — the
    CI/test default."""
    global _store
    if _store is None:
        from app.config import get_settings  # noqa: PLC0415 — avoid import cycle

        settings = get_settings()
        if settings.integration_mode == "live":
            _store = _build_redis_store(settings.redis_url) or InMemoryLLMCapStore()
        else:
            _store = InMemoryLLMCapStore()
    return _store


def _build_redis_store(redis_url: str) -> RedisLLMCapStore | None:
    """A ``RedisLLMCapStore`` over ``redis.asyncio`` that binds on first use and
    re-binds while unbound. None only when the redis package itself is missing —
    there is then nothing to bind to, ever."""
    try:
        import redis.asyncio as redis_asyncio  # noqa: PLC0415 — lazy/guarded import
    except Exception:  # noqa: BLE001 — redis missing → degrade to in-memory
        logger.warning(
            "LLM cap: redis.asyncio is unavailable; using in-memory cap (degraded)",
            exc_info=True,
        )
        return None

    def _client() -> Any:
        return redis_asyncio.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
        )

    return RedisLLMCapStore(client_factory=_client)


def admit_llm_call(admin_id: uuid.UUID, *, cap: int = LLM_CAP_PER_MINUTE) -> None:
    """Admit one LLM call for admin_id SYNCHRONOUSLY; raise LLMCapExceeded if over cap.

    In-process stores only (the seedtest drives an ``InMemoryLLMCapStore`` with it):
    a synchronous caller cannot await Redis without blocking the event loop, so the
    Redis store counts these in-process. The router awaits ``aadmit_llm_call``."""
    store = get_llm_cap_store()
    if not store.admit(str(admin_id), cap=cap):
        raise LLMCapExceeded(admin_id)


async def aadmit_llm_call(admin_id: uuid.UUID, *, cap: int = LLM_CAP_PER_MINUTE) -> None:
    """Admit one LLM call for admin_id; raise LLMCapExceeded if over the cap.

    Awaited by the LLMRouter (round 7 WP-6 item 6) before a turn's first model call,
    and before every call made outside a turn. Raises ``LLMCapExceeded`` when the
    tenant is at or over the 50/min cap (Architecture §9 C9 / §4.6)."""
    store = get_llm_cap_store()
    if not await store.aadmit(str(admin_id), cap=cap):
        raise LLMCapExceeded(admin_id)
