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
"""

from __future__ import annotations

import time
import uuid

# Default: 50 LLM calls per admin_id per 60-second window (Architecture §9 C9).
LLM_CAP_PER_MINUTE: int = 50
LLM_CAP_WINDOW_SECONDS: float = 60.0


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


_store: InMemoryLLMCapStore | None = None


def get_llm_cap_store() -> InMemoryLLMCapStore:
    """Return the process-global LLM cap store (in-memory; Redis swaps in for prod)."""
    global _store
    if _store is None:
        _store = InMemoryLLMCapStore()
    return _store


def admit_llm_call(admin_id: uuid.UUID, *, cap: int = LLM_CAP_PER_MINUTE) -> None:
    """Admit one LLM call for admin_id; raise LLMCapExceeded if over the cap.

    Called by the LLMRouter (or orchestrator) before every LLM ``complete()`` call.
    Raises ``LLMCapExceeded`` when the tenant is at or over the 50/min cap
    (Architecture §9 C9 / §4.6)."""
    store = get_llm_cap_store()
    if not store.admit(str(admin_id), cap=cap):
        raise LLMCapExceeded(admin_id)
