"""All-channel abuse controls + rate limiting — pre-budget (Architecture §3.1.5).

The unifying principle: abuse traffic is rejected BEFORE the conversation budget
gate (§3.4.1b) and before any LLM call, so an attack can never burn a tenant's
budget or run up cost. Controls are platform-managed and tier-uniform.

  - Per-source token bucket (burst + sustained), keyed by the channel's source
    identifier (E.164 for SMS/voice; from-address for email; Meta sender id for
    WhatsApp/IG/Messenger). The widget keys on TRUSTED client identity — the ALB-
    reported client IP plus the server-side session id once it has proven to resolve
    to an open session — never on a caller-supplied value alone (a rotating
    client-chosen id must not mint a fresh bucket, §3.1.5 denial-of-wallet).
  - Per-source NEW-SESSION cap for the widget: opening a conversation is the
    billable event, so it is limited separately from sending a message into one.
  - Per-tenant inbound ceiling across all channels combined (denial-of-wallet backstop).
  - Per-source auto-block after repeated abuse; blocks EXPIRE (24h) so a shared
    office IP is never punished forever, and every counter is evicted when idle so
    the in-memory store cannot grow without bound.

Backing store: ``RedisTokenBucketStore`` in live mode (atomic INCR + EXPIRE, shared
across every task so the ceilings hold platform-wide and survive a deploy).
``TokenBucketStore`` is the in-memory implementation used in mock/CI and as the
per-process DEGRADED fallback when Redis is unreachable — the cap stays in force,
narrowed to one process, and ``store_kind()`` lets ``/healthz`` say so (§4.5).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelLimits:
    burst: int  # max tokens
    refill_seconds: float  # seconds per 1 token sustained
    daily_cap: int  # per-source per-day


# Per-channel-class limits (§3.1.5 table).
LIMITS: dict[str, ChannelLimits] = {
    "widget": ChannelLimits(burst=5, refill_seconds=3.0, daily_cap=2000),
    "sms": ChannelLimits(burst=5, refill_seconds=5.0, daily_cap=500),
    "voice": ChannelLimits(burst=3, refill_seconds=10.0, daily_cap=200),
    "email": ChannelLimits(burst=10, refill_seconds=2.0, daily_cap=1000),
    "whatsapp": ChannelLimits(burst=5, refill_seconds=5.0, daily_cap=500),
    "messenger": ChannelLimits(burst=5, refill_seconds=5.0, daily_cap=500),
    "instagram": ChannelLimits(burst=5, refill_seconds=5.0, daily_cap=500),
    # Legacy combined id (pre-§3.1.2 split) — kept so any in-flight caller stays limited.
    "instagram_messenger": ChannelLimits(burst=5, refill_seconds=5.0, daily_cap=500),
    # Widget, per TRUSTED client IP per instance, across every session that IP drives
    # (§3.1.5 "separate per-IP cap across sessions"). Looser than the per-session
    # bucket so a shared office NAT with several live chats is not throttled, but it
    # is the ceiling a client-controlled session id can never escape.
    "widget_ip": ChannelLimits(burst=15, refill_seconds=1.0, daily_cap=4000),
    # Widget, NEW conversations per client IP per instance. Opening a session is the
    # billable event (§3.4.1b fires on the first model call of a NEW session), so it
    # is capped independently of message volume. One human opens one chat; five in
    # fifteen seconds from one address is not a visitor.
    "widget_session_open": ChannelLimits(burst=5, refill_seconds=15.0, daily_cap=300),
    # Widget bootstrap (page view → render directive) per client IP per instance.
    # Unauthenticated and DB-backed, so it needs its own edge ceiling.
    "widget_bootstrap": ChannelLimits(burst=10, refill_seconds=2.0, daily_cap=3000),
    # Widget transcript polling (takeover replies arrive on the next poll, §3.4.12):
    # one open panel polls a few times a minute; this only stops a scraper.
    "widget_poll": ChannelLimits(burst=20, refill_seconds=1.0, daily_cap=30000),
    # Not a conversational channel and not in the §3.1.5 table: the marketing contact
    # form (ruling 1). It shares the bucket rather than growing a second limiter, but its
    # limits are an order of magnitude tighter — a human sends one enquiry, not five a
    # minute, and there is no tenant behind it to absorb the abuse.
    "contact": ChannelLimits(burst=3, refill_seconds=60.0, daily_cap=20),
    # Destination-claim CONFLICTS (c21, not conversational): repeated 409s on the
    # number-designate route are an enumeration oracle for which numbers are
    # claimed. Keyed per admin; contact-form-tight because a legitimate owner
    # hits this at most once or twice by accident.
    "destination_claim": ChannelLimits(burst=3, refill_seconds=60.0, daily_cap=20),
    # Owner-triggered test texts to an escalation contact (F084): a handful an hour.
    "escalation_test_sms": ChannelLimits(burst=3, refill_seconds=600.0, daily_cap=20),
    # Account-export downloads per signed link (adversarial review R-WP2-2): the
    # door is unauthenticated by design (the token is the authorization), so a
    # leaked or replayed link must not become an unbounded audit-row + presign mill.
    # A person downloads once or twice; a browser that retries is covered by the burst.
    "export_download": ChannelLimits(burst=5, refill_seconds=60.0, daily_cap=50),
    # Sign-in and password-reset throttles (2026-09-05 audit F056/F068): per email
    # AND per source address, so neither a single-account brute force nor a
    # many-account spray runs unbounded. A reset request is throttled whether or
    # not the address exists, so the throttle itself enumerates nothing (§3.7.1a).
    "auth_login_email": ChannelLimits(burst=10, refill_seconds=60.0, daily_cap=100),
    "auth_login_ip": ChannelLimits(burst=30, refill_seconds=2.0, daily_cap=1000),
    "auth_forgot_email": ChannelLimits(burst=3, refill_seconds=300.0, daily_cap=10),
    "auth_forgot_ip": ChannelLimits(burst=10, refill_seconds=60.0, daily_cap=100),
    # Voice status callbacks (C11, not conversational): the finalize-at-hangup
    # route bypasses pipeline.ingest, so it gets its own per-destination-number
    # bucket. Legit volume is one callback per call plus provider retries —
    # generous headroom over the voice channel's own 200/day cap.
    "voice_status": ChannelLimits(burst=10, refill_seconds=2.0, daily_cap=2000),
}

# Per-tenant cross-channel inbound ceiling (messages/minute) — denial-of-wallet backstop.
TENANT_INBOUND_PER_MINUTE = 300

AUTO_BLOCK_ABUSE_THRESHOLD = 50  # repeated rejects from one source → auto-block
AUTO_BLOCK_SECONDS = 24 * 3600  # a block is a cool-down, not a life sentence
IDLE_EVICT_SECONDS = 24 * 3600  # in-memory entries untouched this long are dropped
_EVICT_EVERY_N_OPS = 500


class RateLimitResult:
    def __init__(self, allowed: bool, reason: str | None = None) -> None:
        self.allowed = allowed
        self.reason = reason


def _utc_day(wall_clock: float) -> str:
    return time.strftime("%Y%m%d", time.gmtime(wall_clock))


class TokenBucketStore:
    """In-memory token-bucket + counters (mock/CI, and the per-process fallback).

    Every structure is bounded: daily counters roll over at UTC midnight, blocks
    expire, and idle entries are evicted on a cadence — the earlier version kept
    every key it ever saw and never reset the day count, so a source that hit its
    daily cap once was denied forever and the dictionaries only ever grew.
    """

    kind = "in_memory"

    def __init__(self) -> None:
        self._tokens: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_ts)
        self._daily: dict[str, tuple[int, str]] = {}  # key -> (count, utc_day)
        self._tenant_minute: dict[str, tuple[int, float]] = {}
        self._abuse: dict[str, tuple[int, float]] = {}  # key -> (count, last_ts)
        self._blocked: dict[str, float] = {}  # key -> block expiry (monotonic)
        self._ops = 0

    def _now(self) -> float:
        return time.monotonic()

    def _wall(self) -> float:
        return time.time()

    def _maybe_evict(self, now: float) -> None:
        self._ops += 1
        if self._ops % _EVICT_EVERY_N_OPS:
            return
        cutoff = now - IDLE_EVICT_SECONDS
        today = _utc_day(self._wall())
        self._tokens = {k: v for k, v in self._tokens.items() if v[1] >= cutoff}
        self._daily = {k: v for k, v in self._daily.items() if v[1] == today}
        self._tenant_minute = {k: v for k, v in self._tenant_minute.items() if now - v[1] < 120.0}
        self._abuse = {k: v for k, v in self._abuse.items() if v[1] >= cutoff}
        self._blocked = {k: exp for k, exp in self._blocked.items() if exp > now}

    def check_source(self, channel: str, source_id: str) -> RateLimitResult:
        limits = LIMITS.get(channel)
        if limits is None:
            return RateLimitResult(True)
        key = f"{channel}:{source_id}"
        now = self._now()
        self._maybe_evict(now)
        expiry = self._blocked.get(key)
        if expiry is not None:
            if expiry > now:
                return RateLimitResult(False, "auto_blocked")
            del self._blocked[key]

        tokens, last = self._tokens.get(key, (float(limits.burst), now))
        # Refill.
        elapsed = max(0.0, now - last)
        tokens = min(float(limits.burst), tokens + elapsed / limits.refill_seconds)
        if tokens < 1.0:
            self._record_abuse(key, now)
            return RateLimitResult(False, "rate_limited")
        tokens -= 1.0
        self._tokens[key] = (tokens, now)

        # Daily cap — rolls over at UTC midnight instead of accumulating forever.
        today = _utc_day(self._wall())
        dcount, day = self._daily.get(key, (0, today))
        if day != today:
            dcount = 0
        dcount += 1
        self._daily[key] = (dcount, today)
        if dcount > limits.daily_cap:
            self._record_abuse(key, now)
            return RateLimitResult(False, "daily_cap")
        return RateLimitResult(True)

    def check_tenant(self, admin_id: str) -> RateLimitResult:
        now = self._now()
        count, window = self._tenant_minute.get(admin_id, (0, now))
        if now - window >= 60.0:
            count, window = 0, now
        count += 1
        self._tenant_minute[admin_id] = (count, window)
        if count > TENANT_INBOUND_PER_MINUTE:
            return RateLimitResult(False, "tenant_inbound_ceiling")
        return RateLimitResult(True)

    def _record_abuse(self, key: str, now: float) -> None:
        n, _ = self._abuse.get(key, (0, now))
        n += 1
        self._abuse[key] = (n, now)
        if n >= AUTO_BLOCK_ABUSE_THRESHOLD:
            self._blocked[key] = now + AUTO_BLOCK_SECONDS
            self._abuse.pop(key, None)

    def record_abuse(self, channel: str, source_id: str) -> None:
        """An abuse signal from OUTSIDE the limiter (a detected prompt injection,
        2026-09-05 audit F086) counts toward the same per-source auto-block a
        rate-limit refusal does."""
        self._record_abuse(f"{channel}:{source_id}", self._now())

    def is_blocked(self, channel: str, source_id: str) -> bool:
        expiry = self._blocked.get(f"{channel}:{source_id}")
        return expiry is not None and expiry > self._now()


class RedisTokenBucketStore:
    """Redis-backed store (live mode) mirroring ``TokenBucketStore``'s interface.

    Every counter is a fixed window implemented with INCR + EXPIRE, which is atomic
    without Lua and therefore identical on every Redis flavour (and on fakeredis in
    tests). The per-source "bucket" is the burst allowance per window of
    ``burst × refill_seconds`` — the same sustained rate as the token bucket, with
    the bounded difference that a window boundary can admit one extra burst. Keys
    carry their own expiry, so nothing accumulates: daily counters die after two
    days, minute counters after two minutes, blocks after ``AUTO_BLOCK_SECONDS``.

    Degradation (cost-control doctrine, §4.5): failing OPEN on a Redis error would
    remove the denial-of-wallet ceiling, so any error degrades to the per-process
    in-memory store — the cap stays, narrowed to one task, and the condition is
    logged once and reported by ``store_kind()``.
    """

    kind = "redis"

    def __init__(self, client: object) -> None:
        self._client = client
        self._fallback = TokenBucketStore()
        self._degraded_logged = False

    def _degraded(self) -> TokenBucketStore:
        if not self._degraded_logged:
            logger.warning(
                "rate limit: redis unavailable; degrading to in-memory per-process counting",
                exc_info=True,
            )
            self._degraded_logged = True
        return self._fallback

    def _incr(self, key: str, ttl: int) -> int:
        n = int(self._client.incr(key))  # type: ignore[attr-defined]
        if n == 1:
            self._client.expire(key, ttl)  # type: ignore[attr-defined]
        return n

    def check_source(self, channel: str, source_id: str) -> RateLimitResult:
        limits = LIMITS.get(channel)
        if limits is None:
            return RateLimitResult(True)
        key = f"{channel}:{source_id}"
        try:
            if self._client.exists(f"rl:b:{key}"):  # type: ignore[attr-defined]
                return RateLimitResult(False, "auto_blocked")
            wall = time.time()
            window = max(1, math.ceil(limits.burst * limits.refill_seconds))
            slot = int(wall // window)
            if self._incr(f"rl:w:{key}:{slot}", window * 2) > limits.burst:
                self._record_abuse(key)
                return RateLimitResult(False, "rate_limited")
            if self._incr(f"rl:d:{key}:{_utc_day(wall)}", 2 * 86400) > limits.daily_cap:
                self._record_abuse(key)
                return RateLimitResult(False, "daily_cap")
            return RateLimitResult(True)
        except Exception:  # noqa: BLE001 — any redis error → degrade, never fail open
            return self._degraded().check_source(channel, source_id)

    def check_tenant(self, admin_id: str) -> RateLimitResult:
        try:
            slot = int(time.time() // 60)
            if self._incr(f"rl:t:{admin_id}:{slot}", 120) > TENANT_INBOUND_PER_MINUTE:
                return RateLimitResult(False, "tenant_inbound_ceiling")
            return RateLimitResult(True)
        except Exception:  # noqa: BLE001
            return self._degraded().check_tenant(admin_id)

    def _record_abuse(self, key: str) -> None:
        if self._incr(f"rl:a:{key}", 86400) >= AUTO_BLOCK_ABUSE_THRESHOLD:
            self._client.set(f"rl:b:{key}", 1, ex=AUTO_BLOCK_SECONDS)  # type: ignore[attr-defined]
            self._client.delete(f"rl:a:{key}")  # type: ignore[attr-defined]

    def record_abuse(self, channel: str, source_id: str) -> None:
        """See ``TokenBucketStore.record_abuse`` (F086). Degrades silently on a
        Redis error — the signal is defence in depth, never a request failure."""
        try:
            self._record_abuse(f"{channel}:{source_id}")
        except Exception:  # noqa: BLE001
            self._degraded().record_abuse(channel, source_id)

    def is_blocked(self, channel: str, source_id: str) -> bool:
        try:
            return bool(self._client.exists(f"rl:b:{channel}:{source_id}"))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return self._degraded().is_blocked(channel, source_id)


_store: TokenBucketStore | RedisTokenBucketStore | None = None


def _build_redis_store(redis_url: str) -> RedisTokenBucketStore | None:
    try:
        import redis  # noqa: PLC0415 — lazy/guarded import (CI has no redis)

        client = redis.Redis.from_url(redis_url, decode_responses=True)
        client.ping()
        return RedisTokenBucketStore(client)
    except Exception:  # noqa: BLE001 — redis missing/unreachable → degrade to in-memory
        logger.warning(
            "rate limit: redis unavailable at bind time; using in-memory store (degraded)",
            exc_info=True,
        )
        return None


def get_store() -> TokenBucketStore | RedisTokenBucketStore:
    """The process-global store: Redis in live mode when reachable, else in-memory."""
    global _store
    if _store is None:
        from app.config import get_settings  # noqa: PLC0415 — avoid import cycle

        settings = get_settings()
        if settings.integration_mode == "live":
            _store = _build_redis_store(settings.redis_url) or TokenBucketStore()
        else:
            _store = TokenBucketStore()
    return _store


def store_kind() -> str:
    """``redis`` when the platform-wide store is bound, else ``in_memory`` (degraded in live)."""
    return get_store().kind


def admit(channel: str, source_id: str, admin_id: str) -> RateLimitResult:
    """Pre-budget admission check. Per-source first, then per-tenant ceiling.

    Returns allowed/denied with a reason. A denial is audited as
    widget_abuse_blocked / channel_abuse_blocked by the caller and never reaches
    the budget gate or an LLM call.
    """
    store = get_store()
    src = store.check_source(channel, source_id)
    if not src.allowed:
        return src
    return store.check_tenant(admin_id)
