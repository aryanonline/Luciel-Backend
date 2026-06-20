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
