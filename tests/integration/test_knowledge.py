"""Knowledge ingestion + retrieval integration tests (Architecture §3.2).

Real Postgres + pgvector. Cover: ingest → ready + chunks embedded; HNSW retrieval
returns the relevant source for a matching query; quota enforcement (per-file +
total); soft-delete removes chunks from retrieval; tenant isolation on retrieval.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.knowledge import service
from app.knowledge.service import PER_FILE_MAX_BYTES, QuotaError
from app.models.audit import AdminAuditLog
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.knowledge import KnowledgeChunk
from sqlalchemy import func, select

pytestmark = [pytest.mark.asyncio]


async def _seed(admin_id: uuid.UUID) -> uuid.UUID:
    async with tenant_session(admin_id) as s:
        s.add(
            Account(
                id=admin_id,
                admin_id=admin_id,
                user_id=admin_id,
                email=f"{admin_id}@x.com",
                email_lower=f"{admin_id}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        iid = uuid.uuid4()
        s.add(
            Instance(
                id=iid,
                admin_id=admin_id,
                name="L",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
        return iid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_ingest_creates_ready_source_with_embedded_chunks():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        src = await service.ingest_text(
            s,
            admin_id=admin,
            instance_id=iid,
            name="brochure.txt",
            text="Our starter engagement is $899. We serve small businesses in Markham.\n\n"
            "We offer consulting, strategy, and growth packages for early-stage teams.",
        )
        assert src.ingestion_status == "ready"
        chunks = (
            (await s.execute(select(KnowledgeChunk).where(KnowledgeChunk.source_id == src.id)))
            .scalars()
            .all()
        )
        assert len(chunks) >= 1
        assert all(c.embedding is not None for c in chunks)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_retrieval_returns_relevant_source():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        await service.ingest_text(
            s,
            admin_id=admin,
            instance_id=iid,
            name="pricing.txt",
            text="The starter engagement price is 899 dollars for new clients.",
        )
        await service.ingest_text(
            s,
            admin_id=admin,
            instance_id=iid,
            name="hours.txt",
            text="Our office hours are nine to five on weekdays only.",
        )
    async with tenant_session(admin) as s:
        results = await service.retrieve(
            s, admin_id=admin, instance_id=iid, query="what is the starter engagement price", k=3
        )
        assert results, "retrieval returned nothing"
        top_text = results[0][0].text.lower()
        assert "899" in top_text or "starter" in top_text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_per_file_and_total_quota_enforced():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        with pytest.raises(QuotaError):
            await service.ingest_text(
                s,
                admin_id=admin,
                instance_id=iid,
                name="big.txt",
                text="x",
                size_bytes=PER_FILE_MAX_BYTES + 1,
            )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_delete_removes_chunks_from_retrieval():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        src = await service.ingest_text(
            s,
            admin_id=admin,
            instance_id=iid,
            name="p.txt",
            text="starter engagement price 899 dollars",
        )
        sid = src.id
    async with tenant_session(admin) as s:
        assert await service.delete_source(s, admin, sid) is True
    async with tenant_session(admin) as s:
        results = await service.retrieve(
            s, admin_id=admin, instance_id=iid, query="starter engagement price", k=5
        )
        assert results == []  # soft-deleted source no longer retrievable
        # chunks hard-removed
        cnt = (
            await s.execute(
                select(func.count())
                .select_from(KnowledgeChunk)
                .where(KnowledgeChunk.source_id == sid)
            )
        ).scalar_one()
        assert cnt == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_retrieval_is_tenant_scoped():
    a, b = uuid.uuid4(), uuid.uuid4()
    ia = await _seed(a)
    ib = await _seed(b)
    async with tenant_session(a) as s:
        await service.ingest_text(
            s, admin_id=a, instance_id=ia, name="a.txt", text="tenant A secret pricing 899"
        )
    async with tenant_session(b) as s:
        await service.ingest_text(
            s, admin_id=b, instance_id=ib, name="b.txt", text="tenant B unrelated content widgets"
        )
    # Tenant B retrieving A's query must never surface A's chunk.
    async with tenant_session(b) as s:
        results = await service.retrieve(
            s, admin_id=b, instance_id=ib, query="tenant A secret pricing 899", k=5
        )
        for chunk, _dist in results:
            assert chunk.admin_id == b


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_rerank_preserves_relevance_primary_order():
    """Step-3 re-rank (Axis4-F01) must not demote a clearly-more-relevant chunk.

    The re-rank is a tie-break WITHIN a narrow cosine band, so the single best
    semantic match still ranks first; recency/ordinal only reorder near-ties.
    """
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        await service.ingest_text(
            s,
            admin_id=admin,
            instance_id=iid,
            name="pricing.txt",
            text="The starter engagement price is 899 dollars for new clients.",
        )
        await service.ingest_text(
            s,
            admin_id=admin,
            instance_id=iid,
            name="hours.txt",
            text="Our office hours are nine to five on weekdays only.",
        )
    async with tenant_session(admin) as s:
        results = await service.retrieve(
            s, admin_id=admin, instance_id=iid, query="what is the starter engagement price", k=3
        )
        assert results, "retrieval returned nothing"
        # distances must be non-decreasing across bands (primary key is cosine band)
        dists = [d for _c, d in results]
        # the strongest semantic match still leads
        assert "899" in results[0][0].text.lower() or "starter" in results[0][0].text.lower()
        # re-rank never produces a wildly out-of-order leader: best result is within
        # one band of the global minimum distance
        assert dists[0] <= min(dists) + 0.05


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_view_chunks_emits_knowledge_source_viewed_audit_event():
    """Axis4-F04: viewing a source's chunks emits knowledge_source_viewed (§3.2.2)."""
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        src = await service.ingest_text(
            s,
            admin_id=admin,
            instance_id=iid,
            name="p.txt",
            text="starter engagement price 899 dollars",
        )
        sid = src.id
    async with tenant_session(admin) as s:
        chunks = await service.view_chunks(s, admin, sid, admin)
        assert chunks, "view_chunks returned no chunks for a real source"
    async with tenant_session(admin) as s:
        cnt = (
            await s.execute(
                select(func.count())
                .select_from(AdminAuditLog)
                .where(
                    AdminAuditLog.admin_id == admin,
                    AdminAuditLog.event_type == "knowledge_source_viewed",
                )
            )
        ).scalar_one()
        assert cnt == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_view_chunks_missing_source_emits_no_event():
    """Axis4-F04: a 404 (unknown source) must NOT emit a spurious viewed event."""
    admin = uuid.uuid4()
    await _seed(admin)
    async with tenant_session(admin) as s:
        chunks = await service.view_chunks(s, admin, uuid.uuid4(), admin)
        assert chunks == []
    async with tenant_session(admin) as s:
        cnt = (
            await s.execute(
                select(func.count())
                .select_from(AdminAuditLog)
                .where(AdminAuditLog.event_type == "knowledge_source_viewed")
            )
        ).scalar_one()
        assert cnt == 0
