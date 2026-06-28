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

import logging
import time
import uuid

logger = logging.getLogger(__name__)

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


class RedisLLMCapStore:
    """Redis-backed per-admin_id LLM cap (production backend, Architecture §9 C9).

    Mirrors ``InMemoryLLMCapStore``'s interface (``admit``, ``current_count``) using a
    fixed 60-second key per admin_id: ``INCR`` the counter then ``EXPIRE`` it on first
    touch. This is the per-minute window the module docstring describes — a strict cap,
    not a refillable bucket. Redis is shared across tasks/containers so the cap holds
    platform-wide (the in-memory store only counts within one process).

    GRACEFUL DEGRADATION (cost-control doctrine, §9 C9 / §4.6): the cap is a cost/abuse
    control, so failing OPEN (removing the cap) on a Redis outage is NOT acceptable —
    one tenant could then monopolize the platform keys. But a Redis outage must not
    drop ALL traffic either. So on any Redis error we log and fall back to a
    process-local ``InMemoryLLMCapStore`` (per-task counting). The cap stays in force,
    degraded to per-process granularity until Redis recovers.

    The redis client is injected (tests pass fakeredis); ``redis`` itself is imported
    lazily only in ``get_llm_cap_store`` so CI imports this module without redis."""

    def __init__(self, client: object) -> None:
        self._client = client
        # Per-process fallback used only while Redis is unreachable (degraded mode).
        self._fallback = InMemoryLLMCapStore()

    @staticmethod
    def _key(admin_id: str) -> str:
        return f"llmcap:{admin_id}"

    def admit(self, admin_id: str, *, cap: int = LLM_CAP_PER_MINUTE) -> bool:
        key = self._key(admin_id)
        try:
            count = int(self._client.incr(key))  # type: ignore[attr-defined]
            if count == 1:
                # First call in this window — start the 60s expiry.
                self._client.expire(key, int(LLM_CAP_WINDOW_SECONDS))  # type: ignore[attr-defined]
            return count <= cap
        except Exception:  # noqa: BLE001 — any redis error → degrade, never fail open
            logger.warning(
                "LLM cap Redis unavailable; degrading to in-memory per-process counting",
                exc_info=True,
            )
            return self._fallback.admit(admin_id, cap=cap)

    def current_count(self, admin_id: str) -> int:
        try:
            raw = self._client.get(self._key(admin_id))  # type: ignore[attr-defined]
            return int(raw) if raw is not None else 0
        except Exception:  # noqa: BLE001 — observability only; degrade quietly
            logger.warning("LLM cap Redis unavailable for current_count", exc_info=True)
            return self._fallback.current_count(admin_id)


_store: InMemoryLLMCapStore | RedisLLMCapStore | None = None


def get_llm_cap_store() -> InMemoryLLMCapStore | RedisLLMCapStore:
    """Return the process-global LLM cap store.

    In ``live`` mode (and only if redis is importable + reachable) this binds the
    ``RedisLLMCapStore`` so the cap holds platform-wide. In every other mode (the
    default ``mock``) it stays ``InMemoryLLMCapStore`` — the CI/test default. If redis
    cannot be reached at bind time in live mode, we log and stay in-memory rather than
    dropping the cap (graceful degradation, see ``RedisLLMCapStore``)."""
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
    """Build a RedisLLMCapStore, or None if redis is unavailable at bind time."""
    try:
        import redis  # noqa: PLC0415 — lazy/guarded import (CI has no redis)

        client = redis.Redis.from_url(redis_url, decode_responses=True)
        client.ping()
        return RedisLLMCapStore(client)
    except Exception:  # noqa: BLE001 — redis missing/unreachable → degrade to in-memory
        logger.warning(
            "LLM cap: redis unavailable at bind time; using in-memory cap (degraded)",
            exc_info=True,
        )
        return None


def admit_llm_call(admin_id: uuid.UUID, *, cap: int = LLM_CAP_PER_MINUTE) -> None:
    """Admit one LLM call for admin_id; raise LLMCapExceeded if over the cap.

    Called by the LLMRouter (or orchestrator) before every LLM ``complete()`` call.
    Raises ``LLMCapExceeded`` when the tenant is at or over the 50/min cap
    (Architecture §9 C9 / §4.6)."""
    store = get_llm_cap_store()
    if not store.admit(str(admin_id), cap=cap):
        raise LLMCapExceeded(admin_id)
