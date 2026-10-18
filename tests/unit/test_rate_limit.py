"""All-channel pre-budget rate limiting + abuse auto-block (Architecture §3.1.5)."""

from __future__ import annotations

from app.channels import rate_limit
from app.channels.rate_limit import (
    AUTO_BLOCK_ABUSE_THRESHOLD,
    TENANT_INBOUND_PER_MINUTE,
    TokenBucketStore,
)


def test_burst_then_rate_limited():
    store = TokenBucketStore()
    src = "+15550001111"
    # SMS burst = 5; the 6th in a burst is rate-limited (no refill time elapsed).
    allowed = 0
    for _ in range(6):
        r = store.check_source("sms", src)
        if r.allowed:
            allowed += 1
    assert allowed == 5


def test_auto_block_after_repeated_abuse():
    store = TokenBucketStore()
    src = "abuser"
    # Drain the bucket, then keep hammering to trigger auto-block.
    for _ in range(5):
        store.check_source("sms", src)
    for _ in range(AUTO_BLOCK_ABUSE_THRESHOLD + 1):
        store.check_source("sms", src)
    assert store.is_blocked("sms", src) is True
    # Once blocked, even a fresh check is denied with auto_blocked.
    r = store.check_source("sms", src)
    assert r.allowed is False and r.reason == "auto_blocked"


def test_tenant_inbound_ceiling_independent_of_source():
    store = TokenBucketStore()
    admin = "tenantB"
    allowed = 0
    for _ in range(TENANT_INBOUND_PER_MINUTE + 5):
        r = store.check_tenant(admin)
        if r.allowed:
            allowed += 1
    assert allowed == TENANT_INBOUND_PER_MINUTE


def test_admit_combines_source_and_tenant():
    # Fresh store via module accessor reset.
    rate_limit._store = TokenBucketStore()
    r = rate_limit.admit("email", "sender@x.com", "tenantC")
    assert r.allowed is True


def _drain_daily(store: TokenBucketStore, channel: str, src: str, cap: int) -> None:
    """Consume `cap` admissions with the bucket kept full (refill faked by the clock)."""
    clock = [0.0]

    def fake_now() -> float:
        clock[0] += 1000.0  # far past any refill interval → bucket is always full
        return clock[0]

    store._now = fake_now  # type: ignore[method-assign]
    for _ in range(cap):
        assert store.check_source(channel, src).allowed


def test_daily_cap_resets_at_utc_midnight(monkeypatch):
    """Audit F033: the daily counter never reset, so a source that hit the cap once was
    denied for the life of the process. The cap is a per-day figure (§3.1.5)."""
    store = TokenBucketStore()
    day = ["20260905"]
    monkeypatch.setattr(rate_limit, "_utc_day", lambda _wall: day[0])
    limits = rate_limit.LIMITS["contact"]
    _drain_daily(store, "contact", "1.2.3.4", limits.daily_cap)
    assert store.check_source("contact", "1.2.3.4").reason == "daily_cap"
    day[0] = "20260906"  # the calendar rolls over
    assert store.check_source("contact", "1.2.3.4").allowed is True


def test_auto_block_expires_instead_of_lasting_forever():
    store = TokenBucketStore()
    src = "shared-office-nat"
    for _ in range(5 + AUTO_BLOCK_ABUSE_THRESHOLD + 1):
        store.check_source("sms", src)
    assert store.is_blocked("sms", src) is True
    # Jump the monotonic clock past the block window: the source is admitted again.
    later = store._now() + rate_limit.AUTO_BLOCK_SECONDS + 1
    store._now = lambda: later  # type: ignore[method-assign]
    assert store.is_blocked("sms", src) is False
    assert store.check_source("sms", src).allowed is True


def test_idle_entries_are_evicted_so_memory_is_bounded():
    """Audit F033: none of the dictionaries were ever evicted — an unbounded leak keyed
    on attacker-chosen source ids. Idle entries now age out on the eviction cadence."""
    store = TokenBucketStore()
    for i in range(300):
        store.check_source("sms", f"+1555000{i:04d}")
    assert len(store._tokens) == 300
    later = store._now() + rate_limit.IDLE_EVICT_SECONDS + 1
    store._now = lambda: later  # type: ignore[method-assign]
    # Enough operations to cross the eviction cadence.
    for _ in range(rate_limit._EVICT_EVERY_N_OPS):
        store.check_source("sms", "+15559999999")
    assert len(store._tokens) == 1
    assert list(store._tokens) == ["sms:+15559999999"]


def test_redis_store_mirrors_the_in_memory_semantics():
    """Audit F167: the module documented a Redis backend that did not exist, so every
    ceiling was per-process and reset on each deploy. The Redis store must enforce the
    same burst / daily / tenant / auto-block outcomes, atomically via INCR+EXPIRE."""
    import fakeredis

    client = fakeredis.FakeRedis(decode_responses=True)
    store = rate_limit.RedisTokenBucketStore(client)
    assert store.kind == "redis"
    allowed = sum(1 for _ in range(6) if store.check_source("sms", "+15550001111").allowed)
    assert allowed == 5, "burst of 5 within one window, the 6th is rate_limited"
    # Tenant ceiling is shared across every source and every process.
    admitted = sum(
        1 for _ in range(TENANT_INBOUND_PER_MINUTE + 5) if store.check_tenant("tenantR").allowed
    )
    assert admitted == TENANT_INBOUND_PER_MINUTE
    # Repeated abuse from one source auto-blocks with an EXPIRING key.
    for _ in range(5 + AUTO_BLOCK_ABUSE_THRESHOLD + 1):
        store.check_source("sms", "abuser")
    assert store.is_blocked("sms", "abuser") is True
    assert client.ttl("rl:b:sms:abuser") > 0
    assert store.check_source("sms", "abuser").reason == "auto_blocked"
    # Every counter carries its own expiry — nothing accumulates in Redis either.
    for key in client.keys("rl:*"):
        assert client.ttl(key) > 0, f"{key} has no expiry"


def test_has_capacity_reads_the_bucket_without_charging_it():
    """Phase-2 security review: the destination-claim gate CONSULTS the bucket before a
    claim is evaluated — a read that charges nothing — and only refusals pay into it.
    Both stores answer it the same way, and an unknown bucket always has capacity."""
    import fakeredis

    for store in (
        TokenBucketStore(),
        rate_limit.RedisTokenBucketStore(fakeredis.FakeRedis(decode_responses=True)),
    ):
        src = "admin-1"
        burst = rate_limit.LIMITS["destination_claim"].burst
        for _ in range(10):
            assert store.has_capacity("destination_claim", src) is True  # reads are free
        for _ in range(burst):
            assert store.check_source("destination_claim", src).allowed
        assert store.has_capacity("destination_claim", src) is False  # spent
        assert store.has_capacity("destination_claim", "admin-2") is True  # per source
        assert store.has_capacity("no_such_bucket", src) is True


def test_has_capacity_is_false_while_a_source_is_blocked_or_over_its_daily_cap(monkeypatch):
    store = TokenBucketStore()
    for _ in range(5 + AUTO_BLOCK_ABUSE_THRESHOLD + 1):
        store.check_source("sms", "abuser")
    assert store.has_capacity("sms", "abuser") is False

    capped = TokenBucketStore()
    monkeypatch.setitem(
        rate_limit.LIMITS,
        "destination_claim",
        rate_limit.ChannelLimits(burst=100, refill_seconds=1.0, daily_cap=2),
    )
    for _ in range(2):
        assert capped.check_source("destination_claim", "a").allowed
    assert capped.has_capacity("destination_claim", "a") is False


def test_crm_backfill_is_a_tight_per_admin_bucket():
    limits = rate_limit.LIMITS["crm_backfill"]
    assert (limits.burst, limits.refill_seconds, limits.daily_cap) == (2, 60.0, 50)


def test_redis_store_degrades_to_in_memory_on_redis_error():
    """A Redis outage must not fail OPEN (cap removed) nor drop all traffic."""

    class _Broken:
        def __getattr__(self, name: str):  # every command raises
            def _boom(*_a, **_k):
                raise ConnectionError("redis down")

            return _boom

    store = rate_limit.RedisTokenBucketStore(_Broken())
    allowed = sum(1 for _ in range(6) if store.check_source("sms", "+15550002222").allowed)
    assert allowed == 5, "the in-memory fallback keeps enforcing the burst"
    assert store.check_tenant("tenantD").allowed is True


def test_store_kind_reports_the_bound_backend(monkeypatch):
    rate_limit._store = TokenBucketStore()
    assert rate_limit.store_kind() == "in_memory"
    import fakeredis

    rate_limit._store = rate_limit.RedisTokenBucketStore(fakeredis.FakeRedis(decode_responses=True))
    assert rate_limit.store_kind() == "redis"
    rate_limit._store = TokenBucketStore()


def test_widget_buckets_exist_for_every_admission_stage():
    """The widget gate keys on trusted identity across four buckets (§3.1.5)."""
    for channel in (
        "widget",
        "widget_ip",
        "widget_session_open",
        "widget_bootstrap",
        "widget_poll",
    ):
        assert channel in rate_limit.LIMITS, channel
    # Opening a conversation is the billable event: its cap must be tighter than the
    # per-IP message allowance.
    assert rate_limit.LIMITS["widget_session_open"].burst < rate_limit.LIMITS["widget_ip"].burst
