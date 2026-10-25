"""Unit tests for RedisLLMCapStore (Architecture §9 C9 / §4.6; round 7 WP-6 item 6).

Hermetic: no real Redis. ``fakeredis.FakeAsyncRedis`` backs the happy path — the store
must AWAIT a ``redis.asyncio`` client (the synchronous client it used to call blocked
the event loop for every turn's admission). We assert it mirrors the
InMemoryLLMCapStore contract (admit up to cap then reject; per-admin isolation;
current_count; INCR then EXPIRE on first touch), that a Redis failure degrades to
in-memory counting rather than failing open (the cap is a cost control and must never
silently disappear), and that a store whose first bind failed RE-BINDS on a bounded
schedule instead of latching the in-memory store for the life of the task.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import fakeredis
import pytest
from app.runtime import llm_cap
from app.runtime.llm_cap import (
    LLM_CAP_PER_MINUTE,
    LLM_CAP_WINDOW_SECONDS,
    REDIS_REBIND_INTERVAL_SECONDS,
    LLMCapExceeded,
    RedisLLMCapStore,
)


def _store() -> tuple[RedisLLMCapStore, fakeredis.FakeAsyncRedis]:
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    return RedisLLMCapStore(client), client


async def test_admit_allows_up_to_cap_then_rejects() -> None:
    store, _ = _store()
    admin = str(uuid.uuid4())
    cap = 5
    for i in range(cap):
        assert await store.aadmit(admin, cap=cap) is True, f"call {i + 1} must be admitted"
    assert await store.aadmit(admin, cap=cap) is False


async def test_admit_is_per_admin() -> None:
    store, _ = _store()
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    cap = 2
    for _ in range(cap):
        await store.aadmit(a, cap=cap)
    assert await store.aadmit(a, cap=cap) is False
    assert await store.aadmit(b, cap=cap) is True


async def test_current_count_tracks_admitted() -> None:
    store, _ = _store()
    admin = str(uuid.uuid4())
    for _ in range(3):
        await store.aadmit(admin)
    assert await store.acurrent_count(admin) == 3
    # The synchronous, non-blocking read (ingest headroom) sees what this process's
    # own INCR returned — no Redis round-trip on the loop.
    assert store.current_count(admin) == 3


def test_default_cap_constant() -> None:
    assert LLM_CAP_PER_MINUTE == 50


async def test_expire_set_on_first_call() -> None:
    store, client = _store()
    admin = str(uuid.uuid4())
    await store.aadmit(admin)
    ttl = await client.ttl(f"llmcap:{admin}")
    assert 0 < ttl <= 60
    # Only the first touch starts the window: a later admission does not extend it.
    await client.expire(f"llmcap:{admin}", 5)
    await store.aadmit(admin)
    assert 0 < await client.ttl(f"llmcap:{admin}") <= 5


async def test_a_window_that_lost_its_expiry_is_repaired_on_the_next_admission(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Phase 3 review (2026-10-19): the EXPIRE used to be sent only when the INCR
    returned 1. An EXPIRE that failed right after a successful INCR (a 0.5 s socket
    timeout on a latency blip, a task dying between the two) left a key that
    counted forever: the tenant's 50th call EVER was refused, and every call after
    it, until an operator deleted the key. Now every admission carries the TTL read
    in the same round trip and repairs a window without one."""
    store, client = _store()
    admin = str(uuid.uuid4())
    key = f"llmcap:{admin}"
    await store.aadmit(admin)
    await client.persist(key)  # what a lost EXPIRE leaves behind
    assert await client.ttl(key) == -1
    with caplog.at_level(logging.ERROR, logger="app.runtime.llm_cap"):
        assert await store.aadmit(admin) is True
    assert 0 < await client.ttl(key) <= LLM_CAP_WINDOW_SECONDS
    assert any("had no expiry" in r.getMessage() for r in caplog.records)
    # A window that still has its expiry is left alone (no log, no extension).
    await client.expire(key, 5)
    caplog.clear()
    await store.aadmit(admin)
    assert 0 < await client.ttl(key) <= 5
    assert not caplog.records


async def test_the_cap_holds_across_two_tasks_sharing_redis() -> None:
    """Two app tasks = two stores over one Redis: the cap is platform-wide."""
    server = fakeredis.FakeServer()
    one = RedisLLMCapStore(fakeredis.FakeAsyncRedis(server=server, decode_responses=True))
    two = RedisLLMCapStore(fakeredis.FakeAsyncRedis(server=server, decode_responses=True))
    admin = str(uuid.uuid4())
    assert await one.aadmit(admin, cap=2) is True
    assert await two.aadmit(admin, cap=2) is True
    assert await one.aadmit(admin, cap=2) is False


# --- graceful degradation: Redis failure must NOT remove the cap -----------
class _BrokenRedis:
    def pipeline(self, *_a: object, **_k: object) -> object:
        raise ConnectionError("redis down")

    async def incr(self, *_a: object, **_k: object) -> int:
        raise ConnectionError("redis down")

    async def expire(self, *_a: object, **_k: object) -> bool:
        raise ConnectionError("redis down")

    async def get(self, *_a: object, **_k: object) -> object:
        raise ConnectionError("redis down")


async def test_redis_failure_degrades_to_in_memory_cap() -> None:
    store = RedisLLMCapStore(_BrokenRedis())
    admin = str(uuid.uuid4())
    cap = 3
    # The cap is still enforced via the in-memory fallback (not failing open).
    for _ in range(cap):
        assert await store.aadmit(admin, cap=cap) is True
    assert await store.aadmit(admin, cap=cap) is False
    assert await store.acurrent_count(admin) == cap


# --- re-binding (item 6): the first ping failing no longer latches in-memory ---------
class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _FlakyFactory:
    """Builds async clients over one fake Redis; ``down`` makes PING (and commands
    on clients it builds) fail like an unreachable server."""

    def __init__(self) -> None:
        self.server = fakeredis.FakeServer()
        self.down = True
        self.built = 0
        self.closed = 0

    def __call__(self) -> object:
        self.built += 1
        factory = self

        class _Client(fakeredis.FakeAsyncRedis):
            async def ping(self, *a: object, **k: object) -> bool:
                if factory.down:
                    raise ConnectionError("connection refused")
                return await super().ping(*a, **k)

            async def incr(self, *a: object, **k: object) -> int:
                if factory.down:
                    raise ConnectionError("connection reset")
                return await super().incr(*a, **k)

            def pipeline(self, *a: object, **k: object) -> object:
                # The admission is one pipelined round trip now (INCR + TTL); an
                # unreachable server fails it the same way it fails a lone INCR.
                if factory.down:
                    raise ConnectionError("connection reset")
                return super().pipeline(*a, **k)

            async def aclose(self, *a: object, **k: object) -> None:
                factory.closed += 1
                await super().aclose(*a, **k)

        return _Client(server=self.server, decode_responses=True)


async def test_a_store_whose_first_bind_failed_rebinds_on_a_bounded_schedule() -> None:
    clock, factory = _Clock(), _FlakyFactory()
    store = RedisLLMCapStore(client_factory=factory, clock=clock)
    admin = str(uuid.uuid4())

    # Redis is down when the first turn arrives: counted in-process, cap still holds.
    assert await store.aadmit(admin, cap=2) is True
    assert store.bound is False and factory.built == 1
    # Inside the interval nothing re-dials (a dead Redis costs one timeout per
    # interval, not one per turn).
    clock.now += REDIS_REBIND_INTERVAL_SECONDS / 2
    assert await store.aadmit(admin, cap=2) is True
    assert await store.aadmit(admin, cap=2) is False  # the in-process cap
    assert factory.built == 1

    # Redis recovers; once the interval has passed the store binds again.
    factory.down = False
    clock.now += REDIS_REBIND_INTERVAL_SECONDS
    other = str(uuid.uuid4())
    assert await store.aadmit(other, cap=2) is True
    assert store.bound is True and factory.built == 2
    probe = fakeredis.FakeAsyncRedis(server=factory.server, decode_responses=True)
    assert await probe.get(f"llmcap:{other}") == "1"  # counted platform-wide again


async def test_a_command_failure_drops_the_client_and_rebinds_later() -> None:
    clock, factory = _Clock(), _FlakyFactory()
    factory.down = False
    store = RedisLLMCapStore(client_factory=factory, clock=clock)
    admin = str(uuid.uuid4())
    assert await store.aadmit(admin) is True and store.bound is True

    factory.down = True  # Redis dies mid-life
    assert await store.aadmit(admin) is True  # degraded, never failing open
    assert store.bound is False and factory.closed == 1

    factory.down = False
    clock.now += REDIS_REBIND_INTERVAL_SECONDS + 1
    assert await store.aadmit(admin) is True
    assert store.bound is True and factory.built == 2


async def test_a_client_bound_on_another_event_loop_is_rebound_not_reused() -> None:
    factory = _FlakyFactory()
    factory.down = False
    store = RedisLLMCapStore(client_factory=factory)
    admin = str(uuid.uuid4())
    await store.aadmit(admin)
    # Simulate a second asyncio.run(): the bound client belongs to a dead loop.
    dead_loop = asyncio.new_event_loop()
    store._client_loop = dead_loop
    try:
        await store.aadmit(admin)
    finally:
        dead_loop.close()
    assert factory.built == 2 and store.bound is True


def test_the_synchronous_read_ages_out_with_the_window() -> None:
    clock = _Clock()
    store = RedisLLMCapStore(client_factory=None, clock=clock)
    store._observed["a"] = (40, clock.now)
    assert store.current_count("a") == 40
    clock.now += LLM_CAP_WINDOW_SECONDS + 1
    assert store.current_count("a") == 0


async def test_live_mode_builds_a_rebinding_async_store_even_with_redis_down(monkeypatch):
    """The production builder: redis.asyncio against a port nothing listens on. The
    store is NOT latched to in-memory — it is a RedisLLMCapStore, unbound, counting
    in-process, and the router's admission still raises at the cap."""
    store = llm_cap._build_redis_store("redis://127.0.0.1:1/0")
    assert isinstance(store, RedisLLMCapStore)
    clock = _Clock()
    store._clock = clock
    monkeypatch.setattr(llm_cap, "_store", store)
    admin = uuid.uuid4()
    for _ in range(3):
        await llm_cap.aadmit_llm_call(admin, cap=3)
    with pytest.raises(LLMCapExceeded):
        await llm_cap.aadmit_llm_call(admin, cap=3)
    assert store.bound is False
