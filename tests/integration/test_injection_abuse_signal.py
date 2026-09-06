"""A detected prompt injection feeds the per-source auto-block (2026-09-05 audit F086).

Detection is monitoring-only for the turn it fires on (§3.4.14) — the untrusted-data
delimiting is the boundary, not the heuristic. But a visitor who keeps probing is
abuse, and abuse used to be counted only by the rate limiter: fifty injection
attempts at a polite pace were fifty audit rows and fifty LLM calls. Now they count
toward the same auto-block a flood earns, so the edge refuses the source before any
runtime work.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels import rate_limit
from app.channels.rate_limit import AUTO_BLOCK_ABUSE_THRESHOLD
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.runtime.orchestrator import Orchestrator
from sqlalchemy import func, select


def test_an_external_abuse_signal_reaches_the_same_block_a_flood_does():
    store = rate_limit.TokenBucketStore()
    for _ in range(AUTO_BLOCK_ABUSE_THRESHOLD - 1):
        store.record_abuse("widget", "sess:203.0.113.9")
    assert store.is_blocked("widget", "sess:203.0.113.9") is False
    store.record_abuse("widget", "sess:203.0.113.9")
    assert store.is_blocked("widget", "sess:203.0.113.9") is True
    # Keyed the way check_source keys: the next admission for this source is refused.
    assert store.check_source("widget", "sess:203.0.113.9").allowed is False
    # Another visitor on the same instance is not.
    assert store.check_source("widget", "sess:198.51.100.4").allowed is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_orchestrator_records_injection_attempts_against_the_source():
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    source = ("widget", f"{sid}:203.0.113.9")
    async with tenant_session(admin) as s:
        orch = Orchestrator(
            session=s,
            router=None,  # type: ignore[arg-type]  # never reached by the audit path
            retriever=None,  # type: ignore[arg-type]
            admin_id=admin,
            instance_id=iid,
            enabled_channels={"widget"},
            has_payment_method=False,
            abuse_source=source,
        )
        for _ in range(AUTO_BLOCK_ABUSE_THRESHOLD):
            await orch._audit_injection(sid)

    assert rate_limit.get_store().is_blocked(*source) is True
    async with tenant_session(admin) as s:
        rows = await s.scalar(
            select(func.count())
            .select_from(AdminAuditLog)
            .where(
                AdminAuditLog.admin_id == admin,
                AdminAuditLog.event_type == "prompt_injection_detected",
            )
        )
    assert rows == AUTO_BLOCK_ABUSE_THRESHOLD  # every attempt is still audited


@pytest.mark.asyncio
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_without_a_known_source_the_detection_only_audits():
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        orch = Orchestrator(
            session=s,
            router=None,  # type: ignore[arg-type]
            retriever=None,  # type: ignore[arg-type]
            admin_id=admin,
            instance_id=iid,
            enabled_channels={"widget"},
            has_payment_method=False,
        )
        await orch._audit_injection(sid)
    # Nothing to block on — and nothing crashed.
    assert rate_limit.get_store().is_blocked("widget", "") is False
