"""Regression tests for Axis11-F01: per-admin LLM concurrency cap (50/min).

Architecture §9 C9 / §4.6: each tenant is capped at 50 LLM calls per 60-second
sliding window. Exceeding the cap raises LLMCapExceeded.
"""

from __future__ import annotations

import uuid

import pytest
from app.runtime.llm_cap import (
    LLM_CAP_PER_MINUTE,
    InMemoryLLMCapStore,
    LLMCapExceeded,
    admit_llm_call,
)

# ---------------------------------------------------------------------------
# InMemoryLLMCapStore unit tests
# ---------------------------------------------------------------------------


def test_llm_cap_default_is_50() -> None:
    """Architecture §9 C9: default cap must be exactly 50 per minute."""
    assert LLM_CAP_PER_MINUTE == 50


def test_admit_allows_up_to_cap() -> None:
    """Axis11-F01: store must admit exactly cap calls and then reject."""
    store = InMemoryLLMCapStore()
    admin = str(uuid.uuid4())
    cap = 5  # use a small cap for speed

    for i in range(cap):
        ok = store.admit(admin, cap=cap)
        assert ok is True, f"call {i + 1} of {cap} must be admitted"

    # The (cap+1)-th call must be rejected.
    assert store.admit(admin, cap=cap) is False, "call over the cap must be denied"


def test_admit_does_not_affect_other_admins() -> None:
    """Axis11-F01: cap is per-admin_id — one tenant exhausted must not block another."""
    store = InMemoryLLMCapStore()
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    cap = 3

    for _ in range(cap):
        store.admit(a, cap=cap)
    # a is now exhausted
    assert store.admit(a, cap=cap) is False, "exhausted admin must be denied"
    # b is unaffected
    assert store.admit(b, cap=cap) is True, "other admin must not be affected"


def test_current_count_reflects_admitted_calls() -> None:
    """InMemoryLLMCapStore.current_count returns in-window call count."""
    store = InMemoryLLMCapStore()
    admin = str(uuid.uuid4())
    for _ in range(7):
        store.admit(admin)
    assert store.current_count(admin) == 7


def test_sliding_window_evicts_expired_timestamps() -> None:
    """Axis11-F01: sliding-window must evict calls older than 60 s."""
    import time as _time

    store = InMemoryLLMCapStore()
    admin = str(uuid.uuid4())
    cap = 2

    # Inject two "old" timestamps directly, outside the window.
    old_ts = _time.monotonic() - 65  # 65 s ago — outside 60 s window
    store._calls[admin] = [old_ts, old_ts]

    # current_count must see 0 (evicted from the window).
    assert store.current_count(admin) == 0

    # And both calls fit in the cap again.
    assert store.admit(admin, cap=cap) is True
    assert store.admit(admin, cap=cap) is True
    assert store.admit(admin, cap=cap) is False  # 3rd is over cap


# ---------------------------------------------------------------------------
# admit_llm_call() function tests
# ---------------------------------------------------------------------------


def test_admit_llm_call_raises_on_overflow() -> None:
    """Axis11-F01: admit_llm_call must raise LLMCapExceeded when cap is reached."""
    # Use a fresh per-test store to avoid cross-test state.
    store = InMemoryLLMCapStore()
    admin = uuid.uuid4()
    cap = 3

    # Admit up to the cap using the store directly (bypass global singleton).
    for _ in range(cap):
        store.admit(str(admin), cap=cap)

    # Now mock the global store to this fresh store and call admit_llm_call.
    import app.runtime.llm_cap as llm_cap_module  # noqa: PLC0415

    original = llm_cap_module._store
    llm_cap_module._store = store
    try:
        with pytest.raises(LLMCapExceeded) as exc_info:
            admit_llm_call(admin, cap=cap)
        assert exc_info.value.admin_id == admin
    finally:
        llm_cap_module._store = original


def test_admit_llm_call_passes_below_cap() -> None:
    """Axis11-F01: admit_llm_call must not raise when under the cap."""
    import app.runtime.llm_cap as llm_cap_module  # noqa: PLC0415

    store = InMemoryLLMCapStore()
    original = llm_cap_module._store
    llm_cap_module._store = store
    try:
        admin = uuid.uuid4()
        # Should not raise — first call is always under cap.
        admit_llm_call(admin, cap=50)
    finally:
        llm_cap_module._store = original


def test_llm_cap_exceeded_str() -> None:
    """LLMCapExceeded message includes the admin_id."""
    admin = uuid.uuid4()
    exc = LLMCapExceeded(admin)
    assert str(admin) in str(exc)


# ---------------------------------------------------------------------------
# The unit the cap counts is the customer TURN (round 7 WP-2, item 4)
# ---------------------------------------------------------------------------
def _bind_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryLLMCapStore:
    import app.runtime.llm_cap as llm_cap_module  # noqa: PLC0415

    store = InMemoryLLMCapStore()
    monkeypatch.setattr(llm_cap_module, "_store", store)
    return store


@pytest.mark.asyncio
async def test_a_turn_admits_the_cap_once_however_many_calls_it_makes(monkeypatch) -> None:
    """A tool-using turn makes up to five model calls; it is ONE of the fifty."""
    from app.runtime.llm_router import LLMRouter  # noqa: PLC0415
    from app.runtime.testing import EchoModel  # noqa: PLC0415

    store = _bind_store(monkeypatch)
    admin = uuid.uuid4()
    router = LLMRouter(primary=EchoModel(), fallback=EchoModel(), admin_id=admin)
    with router.turn():
        for _ in range(5):
            await router.complete("sys", [{"role": "user", "content": "hi"}])
    assert store.current_count(str(admin)) == 1
    # Outside a turn (ingest-time callers) every call is admitted on its own, as before.
    await router.complete("sys", [{"role": "user", "content": "hi"}])
    assert store.current_count(str(admin)) == 2
    # A call on behalf of ANOTHER tenant inside a turn pays under that tenant. The
    # turn's own admission is paid by its own first call (round 7 WP-6 item 6: the
    # sync ``with`` cannot await the store at entry), so a turn that only ever called
    # for another tenant charged its own tenant nothing.
    other = uuid.uuid4()
    with router.turn():
        await router.complete("sys", [{"role": "user", "content": "hi"}], admin_id=other)
    assert store.current_count(str(other)) == 1
    assert store.current_count(str(admin)) == 2
    with router.turn():
        await router.complete("sys", [{"role": "user", "content": "hi"}], admin_id=other)
        await router.complete("sys", [{"role": "user", "content": "hi"}])
    assert store.current_count(str(other)) == 2
    assert store.current_count(str(admin)) == 3


@pytest.mark.asyncio
async def test_an_exhausted_cap_refuses_at_the_turn_boundary_before_any_model_call(
    monkeypatch,
) -> None:
    from app.runtime.llm_router import LLMRouter  # noqa: PLC0415
    from app.runtime.testing import EchoModel  # noqa: PLC0415

    store = _bind_store(monkeypatch)
    admin = uuid.uuid4()
    for _ in range(LLM_CAP_PER_MINUTE):
        assert store.admit(str(admin)) is True

    class _Counting(EchoModel):
        calls = 0

        async def complete(self, *a, **kw):  # type: ignore[override]
            _Counting.calls += 1
            return await super().complete(*a, **kw)

    router = LLMRouter(primary=_Counting(), fallback=EchoModel(), admin_id=admin)
    with pytest.raises(LLMCapExceeded), router.turn():
        await router.complete("sys", [{"role": "user", "content": "hi"}])
    assert _Counting.calls == 0
    # The failed turn left no admission state behind: later turns start clean.
    assert router._in_turn is False


def test_a_turn_without_a_bound_tenant_is_a_no_op(monkeypatch) -> None:
    from app.runtime.llm_router import LLMRouter  # noqa: PLC0415
    from app.runtime.testing import EchoModel  # noqa: PLC0415

    store = _bind_store(monkeypatch)
    router = LLMRouter(primary=EchoModel(), fallback=EchoModel())
    with router.turn(), router.turn():  # nested is fine too
        pass
    assert store._calls == {}


# ---------------------------------------------------------------------------
# Admission is awaited (round 7 WP-6, item 6)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_async_with_turn_pays_at_entry_before_any_model_call(monkeypatch) -> None:
    from app.runtime.llm_router import LLMRouter  # noqa: PLC0415
    from app.runtime.testing import EchoModel  # noqa: PLC0415

    store = _bind_store(monkeypatch)
    admin = uuid.uuid4()
    router = LLMRouter(primary=EchoModel(), fallback=EchoModel(), admin_id=admin)
    async with router.turn():
        assert store.current_count(str(admin)) == 1  # paid at entry
        for _ in range(3):
            await router.complete("sys", [{"role": "user", "content": "hi"}])
    assert store.current_count(str(admin)) == 1

    for _ in range(LLM_CAP_PER_MINUTE - 1):
        assert store.admit(str(admin)) is True
    with pytest.raises(LLMCapExceeded):
        async with router.turn():
            raise AssertionError("an over-cap turn must not enter the block")
    assert router._in_turn is False


@pytest.mark.asyncio
async def test_a_sync_turn_that_makes_no_model_call_costs_nothing(monkeypatch) -> None:
    """The orchestrator's ``with`` form pays in the first ``complete()``: a turn that
    ends before any model call (an exception, an early return) spends no admission."""
    from app.runtime.llm_router import LLMRouter  # noqa: PLC0415
    from app.runtime.testing import EchoModel  # noqa: PLC0415

    store = _bind_store(monkeypatch)
    admin = uuid.uuid4()
    router = LLMRouter(primary=EchoModel(), fallback=EchoModel(), admin_id=admin)
    with router.turn():
        pass
    assert store.current_count(str(admin)) == 0


@pytest.mark.asyncio
async def test_the_router_awaits_the_store_never_a_blocking_call(monkeypatch) -> None:
    """A store whose SYNCHRONOUS admit blows up proves the request path only awaits."""
    import app.runtime.llm_cap as llm_cap_module  # noqa: PLC0415
    from app.runtime.llm_router import LLMRouter  # noqa: PLC0415
    from app.runtime.testing import EchoModel  # noqa: PLC0415

    class _AsyncOnly(InMemoryLLMCapStore):
        awaited = 0

        def admit(self, admin_id: str, *, cap: int = LLM_CAP_PER_MINUTE) -> bool:
            raise AssertionError("the event loop must never block on a sync admission")

        async def aadmit(self, admin_id: str, *, cap: int = LLM_CAP_PER_MINUTE) -> bool:
            _AsyncOnly.awaited += 1
            return True

    monkeypatch.setattr(llm_cap_module, "_store", _AsyncOnly())
    admin = uuid.uuid4()
    router = LLMRouter(primary=EchoModel(), fallback=EchoModel(), admin_id=admin)
    with router.turn():
        await router.complete("sys", [{"role": "user", "content": "hi"}])
        await router.complete("sys", [{"role": "user", "content": "hi"}])
    await router.complete("sys", [{"role": "user", "content": "hi"}])  # outside a turn
    assert _AsyncOnly.awaited == 2
