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
