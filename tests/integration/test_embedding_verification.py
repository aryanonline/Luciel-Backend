"""A stored vector must match its own text (round 6 F121b, 2026-09-13 dev walk).

The label check (F121) re-embeds chunks whose ``embedding_model`` is not the current
pin. It cannot see a vector that carries the current name but came from another
embedder — migration 0058 backfilled every unlabelled row with the live model's name —
nor a vector that is missing. Such a chunk sits past the live relevance floor on every
query: the owner's knowledge is silently dead. The verify pass checks a bounded batch
against a fresh embedding of the chunk's own text and repairs what disagrees.
"""

from __future__ import annotations

import math
import uuid

import pytest
from app.db.session import tenant_session
from app.knowledge import service
from app.knowledge.embeddings import get_embedder
from app.models.instance import Instance, LucielState
from app.models.knowledge import KnowledgeChunk
from app.runtime.embeddings import EMBED_DIM
from sqlalchemy import select, update


async def _seed(admin: uuid.UUID) -> uuid.UUID:
    iid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(Instance(id=iid, admin_id=admin, name="L", state=LucielState.active))
        await s.flush()
    return iid


def _garbage_vector() -> list[float]:
    """A unit vector no text embeds to: the dev walk's mislabelled hash vector, in spirit."""
    vec = [0.0] * EMBED_DIM
    vec[EMBED_DIM - 1] = 1.0
    return vec


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_vector_that_does_not_match_its_text_is_repaired_even_with_the_right_label():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        for i, text in enumerate(("Open 9-5 weekdays.", "Closed Sundays.", "Whitening $349.")):
            await service.ingest_text(
                s, admin_id=admin, instance_id=iid, name=f"d{i}", text=text, origin="paste"
            )
        bad = (
            await s.execute(
                select(KnowledgeChunk.id).where(
                    KnowledgeChunk.admin_id == admin, KnowledgeChunk.text == "Closed Sundays."
                )
            )
        ).scalar_one()
        # The label says "current model"; the vector says nothing of the sort.
        await s.execute(
            update(KnowledgeChunk)
            .where(KnowledgeChunk.id == bad)
            .values(embedding=_garbage_vector(), embedding_model=get_embedder().model_name)
        )
        missing = (
            await s.execute(
                select(KnowledgeChunk.id).where(
                    KnowledgeChunk.admin_id == admin, KnowledgeChunk.text == "Whitening $349."
                )
            )
        ).scalar_one()
        await s.execute(
            update(KnowledgeChunk).where(KnowledgeChunk.id == missing).values(embedding=None)
        )

    # The label-based pass sees nothing wrong.
    async with tenant_session(admin) as s:
        assert await service.reembed_outdated_chunks(s, admin_id=admin) == 0

    async with tenant_session(admin) as s:
        checked, repaired = await service.verify_chunk_embeddings(s, admin_id=admin)
        assert (checked, repaired) == (3, 2)
        rows = (
            (await s.execute(select(KnowledgeChunk).where(KnowledgeChunk.admin_id == admin)))
            .scalars()
            .all()
        )
        assert all(r.embedding_verified_at is not None for r in rows)
        fresh = await get_embedder().embed([r.text for r in rows])
        for row, vec in zip(rows, fresh, strict=True):
            assert row.embedding is not None
            assert service.cosine_distance(list(row.embedding), list(vec)) < 1e-6, row.text
        # Repaired means retrievable again: the once-dead chunk ranks first for its own question.
        hits = await service.retrieve(
            s, admin_id=admin, instance_id=iid, query="Closed Sundays.", k=1
        )
        assert [c.text for c, _d in hits] == ["Closed Sundays."]

    # Stamped rows are not re-checked: the pass converges.
    async with tenant_session(admin) as s:
        assert await service.verify_chunk_embeddings(s, admin_id=admin) == (0, 0)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_verify_pass_is_bounded_per_call():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        for i in range(5):
            await service.ingest_text(
                s, admin_id=admin, instance_id=iid, name=f"d{i}", text=f"Fact {i}.", origin="paste"
            )
    async with tenant_session(admin) as s:
        assert await service.verify_chunk_embeddings(s, admin_id=admin, limit=2) == (2, 0)
        assert await service.verify_chunk_embeddings(s, admin_id=admin, limit=2) == (2, 0)
        assert await service.verify_chunk_embeddings(s, admin_id=admin, limit=2) == (1, 0)
        assert await service.verify_chunk_embeddings(s, admin_id=admin, limit=2) == (0, 0)


def test_cosine_distance_basics():
    assert service.cosine_distance([1.0, 0.0], [1.0, 0.0]) == pytest.approx(0.0)
    assert service.cosine_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)
    assert service.cosine_distance([1.0, 0.0], [0.0, 0.0]) == 1.0
    assert service.cosine_distance([], [1.0]) == 1.0
    assert math.isclose(service.cosine_distance([1.0, 1.0], [2.0, 2.0]), 0.0, abs_tol=1e-9)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_tenant_with_only_pasted_knowledge_is_a_knowledge_sync_candidate():
    """Migration 0065: the sweep that carries the re-embed and verify passes must visit a
    tenant that never connected a sync source — pasted knowledge dies otherwise."""
    from sqlalchemy import text

    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        await service.ingest_text(
            s, admin_id=admin, instance_id=iid, name="hours", text="Open 9-5.", origin="paste"
        )
        rows = (
            (
                await s.execute(
                    text("SELECT admin_id FROM platform_sweep_candidates('knowledge_sync')")
                )
            )
            .scalars()
            .all()
        )
    assert admin in set(rows)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_mock_embedder_never_rewrites_a_live_models_vectors():
    """2026-09-13: a knowledge sweep run from a mock-mode container re-embedded a live
    tenant's chunks with hash vectors. Rows labelled with the live model are left alone
    by both passes when the current embedder is the mock."""
    from app.config import get_settings

    admin = uuid.uuid4()
    iid = await _seed(admin)
    live_model = str(get_settings().knowledge_embedding_model or "text-embedding-3-small")
    async with tenant_session(admin) as s:
        await service.ingest_text(
            s, admin_id=admin, instance_id=iid, name="d", text="Open 9-5.", origin="paste"
        )
        await s.execute(
            update(KnowledgeChunk)
            .where(KnowledgeChunk.admin_id == admin)
            .values(embedding=_garbage_vector(), embedding_model=live_model)
        )
    async with tenant_session(admin) as s:
        assert await service.reembed_outdated_chunks(s, admin_id=admin) == 0
        assert await service.verify_chunk_embeddings(s, admin_id=admin) == (0, 0)
        row = (
            await s.execute(select(KnowledgeChunk).where(KnowledgeChunk.admin_id == admin))
        ).scalar_one()
        assert row.embedding_model == live_model
        assert row.embedding_verified_at is None
        assert list(row.embedding) == _garbage_vector()
