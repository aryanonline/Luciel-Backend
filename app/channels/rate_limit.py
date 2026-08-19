"""All-channel abuse controls + rate limiting — pre-budget (Architecture §3.1.5).

The unifying principle: abuse traffic is rejected BEFORE the conversation budget
gate (§3.4.1b) and before any LLM call, so an attack can never burn a tenant's
budget or run up cost. Controls are platform-managed and tier-uniform.

  - Per-source token bucket (burst + sustained), keyed by the channel's source
    identifier (session+IP+fingerprint for widget; E.164 for SMS/voice; from-address
    for email; Meta sender id for WhatsApp/IG/Messenger).
  - Per-tenant inbound ceiling across all channels combined (denial-of-wallet backstop).
  - Per-source auto-block after repeated abuse.

Backed by Redis (atomic INCR + EXPIRE). A pure in-memory fallback is provided for
tests/CI so the algorithm is exercisable without Redis.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


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
}

# Per-tenant cross-channel inbound ceiling (messages/minute) — denial-of-wallet backstop.
TENANT_INBOUND_PER_MINUTE = 300

AUTO_BLOCK_ABUSE_THRESHOLD = 50  # repeated rejects from one source → auto-block


class RateLimitResult:
    def __init__(self, allowed: bool, reason: str | None = None) -> None:
        self.allowed = allowed
        self.reason = reason


class TokenBucketStore:
    """In-memory token-bucket + counters (test/CI). Redis impl mirrors this API."""

    def __init__(self) -> None:
        self._tokens: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_ts)
        self._daily: dict[str, int] = {}
        self._tenant_minute: dict[str, tuple[int, float]] = {}
        self._abuse: dict[str, int] = {}
        self._blocked: set[str] = set()

    def _now(self) -> float:
        return time.monotonic()

    def check_source(self, channel: str, source_id: str) -> RateLimitResult:
        limits = LIMITS.get(channel)
        if limits is None:
            return RateLimitResult(True)
        key = f"{channel}:{source_id}"
        if key in self._blocked:
            return RateLimitResult(False, "auto_blocked")

        now = self._now()
        tokens, last = self._tokens.get(key, (float(limits.burst), now))
        # Refill.
        elapsed = now - last
        tokens = min(float(limits.burst), tokens + elapsed / limits.refill_seconds)
        if tokens < 1.0:
            self._record_abuse(key)
            return RateLimitResult(False, "rate_limited")
        tokens -= 1.0
        self._tokens[key] = (tokens, now)

        # Daily cap.
        dcount = self._daily.get(key, 0) + 1
        self._daily[key] = dcount
        if dcount > limits.daily_cap:
            self._record_abuse(key)
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

    def _record_abuse(self, key: str) -> None:
        n = self._abuse.get(key, 0) + 1
        self._abuse[key] = n
        if n >= AUTO_BLOCK_ABUSE_THRESHOLD:
            self._blocked.add(key)

    def is_blocked(self, channel: str, source_id: str) -> bool:
        return f"{channel}:{source_id}" in self._blocked


_store: TokenBucketStore | None = None


def get_store() -> TokenBucketStore:
    global _store
    if _store is None:
        _store = TokenBucketStore()
    return _store


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
