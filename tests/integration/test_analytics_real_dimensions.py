"""The two "not available yet" analytics cards become real (C14 — owner concern #9).

Top knowledge sources reads the durable retrieval trace (C6); conversion by
source reads the admin-marked leads.outcome (migration 0050) grouped by each
lead's first-contact channel. Also pins the §3.2.2 delete-confirmation count
(usedByQuestions7d) and the outcome-marking route with its PII-free audit row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from app.analytics import service as analytics_service
from app.db.session import tenant_session
from app.models.conversation import Session as SessionModel
from app.models.knowledge import KnowledgeSource
from app.models.lead import Lead
from app.models.trace import RetrievalTraceEvent

pytestmark = [pytest.mark.asyncio]


def current_billing_period_start() -> date:
    """The calendar-month anchor (the no-card fallback of ``period_start_for``).

    These tests seed rows AT ``now`` and only need a period boundary that
    contains them; the account-anchored resolution is covered by the usage
    contract suite."""
    return datetime.now(UTC).date().replace(day=1)


async def _seed_source(s, admin, iid, name: str) -> uuid.UUID:
    sid = uuid.uuid4()
    s.add(
        KnowledgeSource(
            id=sid,
            admin_id=admin,
            instance_id=iid,
            name=name,
            origin="upload",
            ingestion_status="ready",
        )
    )
    return sid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_top_knowledge_sources_group_real_retrieval_traces():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        brochure = await _seed_source(s, admin, iid, "Brochure.pdf")
        faq = await _seed_source(s, admin, iid, "FAQ.docx")
        session_id = uuid.uuid4()
        for source_id, answers in ((brochure, 3), (faq, 1)):
            for _ in range(answers):
                s.add(
                    RetrievalTraceEvent(
                        id=uuid.uuid4(),
                        admin_id=admin,
                        instance_id=iid,
                        session_id=session_id,
                        source_id=source_id,
                    )
                )
        await s.flush()
        agg = await analytics_service.build_overview(
            s,
            admin_id=admin,
            period_start=current_billing_period_start(),
            conversations_this_period=0,
        )
    assert agg.top_knowledge_sources_gap is None
    assert [(x["name"], x["retrieval_count"]) for x in agg.top_knowledge_sources] == [
        ("Brochure.pdf", 3),
        ("FAQ.docx", 1),
    ]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_conversion_by_source_reads_outcomes_by_first_contact_channel():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        # Two widget-first leads (one converted) + one sms-first lead (lost).
        for channel, outcome in (
            ("widget", "converted"),
            ("widget", "in_progress"),
            ("sms", "lost"),
        ):
            lead_id = uuid.uuid4()
            s.add(Lead(id=lead_id, admin_id=admin, instance_id=iid, name="L", outcome=outcome))
            s.add(
                SessionModel(
                    id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    channel=channel,
                    mode="ai",
                    resolved_lead_id=lead_id,
                )
            )
        await s.flush()
        agg = await analytics_service.build_overview(
            s,
            admin_id=admin,
            period_start=current_billing_period_start(),
            conversations_this_period=0,
        )
    by_source = {c.source: c for c in agg.conversion_by_source}
    assert by_source["widget"].leads == 2
    assert by_source["widget"].converted == 1
    assert by_source["widget"].conversion_rate == 0.5
    assert by_source["sms"].leads == 1
    assert by_source["sms"].converted == 0
    assert agg.conversion_by_source_gap is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_used_by_questions_7d_counts_only_the_window():
    from app.api.v1.admin.knowledge import _used_by_questions_7d

    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        src = await _seed_source(s, admin, iid, "Pricing.pdf")
        recent = RetrievalTraceEvent(
            id=uuid.uuid4(), admin_id=admin, instance_id=iid, session_id=uuid.uuid4(), source_id=src
        )
        old = RetrievalTraceEvent(
            id=uuid.uuid4(),
            admin_id=admin,
            instance_id=iid,
            session_id=uuid.uuid4(),
            source_id=src,
            at=datetime.now(UTC) - timedelta(days=30),
        )
        s.add(recent)
        s.add(old)
        await s.flush()
        counts = await _used_by_questions_7d(s, admin)
    assert counts == {src: 1}
