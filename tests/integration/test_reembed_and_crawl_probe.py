"""The embedding model behind each vector is recorded and brought forward
(2026-09-05 audit F121), and a website crawl is never probed for a credential it
never had (F104).
"""

from __future__ import annotations

import uuid

import pytest
from app.connections import registry
from app.db.session import tenant_session
from app.knowledge import service
from app.knowledge.embeddings import get_embedder
from app.models.instance import Instance, LucielState
from app.models.knowledge import KnowledgeChunk
from sqlalchemy import select, update

pytestmark = [pytest.mark.asyncio]


async def _seed(admin: uuid.UUID) -> uuid.UUID:
    iid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(Instance(id=iid, admin_id=admin, name="L", state=LucielState.active))
        await s.flush()
    return iid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_every_new_chunk_records_the_model_that_embedded_it():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        await service.ingest_text(
            s,
            admin_id=admin,
            instance_id=iid,
            name="hours",
            text="Open 9-5 weekdays.",
            origin="paste",
        )
        models = (
            (
                await s.execute(
                    select(KnowledgeChunk.embedding_model).where(KnowledgeChunk.admin_id == admin)
                )
            )
            .scalars()
            .all()
        )
    assert models and set(models) == {get_embedder().model_name}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_sweep_pass_reembeds_chunks_from_another_model_in_bounded_batches():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        for i in range(3):
            await service.ingest_text(
                s,
                admin_id=admin,
                instance_id=iid,
                name=f"d{i}",
                text=f"Document {i} text.",
                origin="paste",
            )
        await s.execute(
            update(KnowledgeChunk)
            .where(KnowledgeChunk.admin_id == admin)
            .values(embedding_model="text-embedding-ancient")
        )
    async with tenant_session(admin) as s:
        first = await service.reembed_outdated_chunks(s, admin_id=admin, limit=2)
    async with tenant_session(admin) as s:
        second = await service.reembed_outdated_chunks(s, admin_id=admin, limit=2)
        third = await service.reembed_outdated_chunks(s, admin_id=admin, limit=2)
        models = (
            (
                await s.execute(
                    select(KnowledgeChunk.embedding_model).where(KnowledgeChunk.admin_id == admin)
                )
            )
            .scalars()
            .all()
        )
    assert (first, second, third) == (2, 1, 0)
    assert set(models) == {get_embedder().model_name}


def test_a_website_crawl_connection_holds_no_credential_to_lose():
    assert registry.secret_material_expected("website_crawl", "oauth_token") is False
    # An OAuth knowledge provider still must hold its token.
    assert registry.secret_material_expected("google_drive", "oauth_token") is True
