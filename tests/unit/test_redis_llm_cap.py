"""Unit tests for RedisLLMCapStore using fakeredis (Architecture §9 C9 / §4.6).

Hermetic: no real Redis. fakeredis backs the store. We assert it mirrors the
InMemoryLLMCapStore contract (admit up to cap then reject; per-admin isolation;
current_count) and that a Redis failure degrades to in-memory counting rather than
failing open (the cap is a cost control and must never silently disappear).
"""

from __future__ import annotations

import uuid

import fakeredis
from app.runtime.llm_cap import LLM_CAP_PER_MINUTE, RedisLLMCapStore


def _store() -> RedisLLMCapStore:
    return RedisLLMCapStore(fakeredis.FakeStrictRedis(decode_responses=True))


def test_admit_allows_up_to_cap_then_rejects() -> None:
    store = _store()
    admin = str(uuid.uuid4())
    cap = 5
    for i in range(cap):
        assert store.admit(admin, cap=cap) is True, f"call {i + 1} must be admitted"
    assert store.admit(admin, cap=cap) is False


def test_admit_is_per_admin() -> None:
    store = _store()
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    cap = 2
    for _ in range(cap):
        store.admit(a, cap=cap)
    assert store.admit(a, cap=cap) is False
    assert store.admit(b, cap=cap) is True


def test_current_count_tracks_admitted() -> None:
    store = _store()
    admin = str(uuid.uuid4())
    for _ in range(3):
        store.admit(admin)
    assert store.current_count(admin) == 3


def test_default_cap_constant() -> None:
    assert LLM_CAP_PER_MINUTE == 50


def test_expire_set_on_first_call() -> None:
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    store = RedisLLMCapStore(client)
    admin = str(uuid.uuid4())
    store.admit(admin)
    ttl = client.ttl(f"llmcap:{admin}")
    assert 0 < ttl <= 60


# --- graceful degradation: Redis failure must NOT remove the cap -----------
class _BrokenRedis:
    def incr(self, *_a: object, **_k: object) -> int:
        raise ConnectionError("redis down")

    def expire(self, *_a: object, **_k: object) -> bool:
        raise ConnectionError("redis down")

    def get(self, *_a: object, **_k: object) -> object:
        raise ConnectionError("redis down")


def test_redis_failure_degrades_to_in_memory_cap() -> None:
    store = RedisLLMCapStore(_BrokenRedis())
    admin = str(uuid.uuid4())
    cap = 3
    # The cap is still enforced via the in-memory fallback (not failing open).
    for _ in range(cap):
        assert store.admit(admin, cap=cap) is True
    assert store.admit(admin, cap=cap) is False
