"""Contract conformance for /api/v1/admin/knowledge/* (frozen + additive ingest)."""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _ready_account_with_luciel(client) -> uuid.UUID:
    from app.auth import access

    email = f"kb-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    await client.post(
        "/api/v1/admin/luciel", json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"}
    )
    return acct.id


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_quota_shape(client):
    await _ready_account_with_luciel(client)
    r = await client.get("/api/v1/admin/knowledge/quota")
    assert r.status_code == 200
    body = r.json()
    assert body["totalBytes"] == 5_000_000_000
    assert body["perFileMaxBytes"] == 50_000_000
    assert body["usedBytes"] == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_source_list_pages_and_keeps_its_shape(client):
    """WP-8 pagination audit: the list was one unbounded query."""
    await _ready_account_with_luciel(client)
    for name in ("Hours", "Parking"):
        r = await client.post(
            "/api/v1/admin/knowledge/sources/paste",
            json={"name": name, "text": f"{name} are listed on the website."},
        )
        assert r.status_code < 300, r.text
    assert len((await client.get("/api/v1/admin/knowledge/sources")).json()) == 2
    newest = await client.get("/api/v1/admin/knowledge/sources", params={"limit": 1})
    older = await client.get("/api/v1/admin/knowledge/sources", params={"limit": 1, "offset": 1})
    assert [s["name"] for s in newest.json() + older.json()] == ["Parking", "Hours"]
    too_many = await client.get("/api/v1/admin/knowledge/sources", params={"limit": 501})
    assert too_many.status_code == 422


async def test_paste_while_the_embedder_is_down_is_an_honest_503_and_saves_nothing(
    client, monkeypatch, capsys
):
    """2026-09-06 E2E walk (E2E-12): OpenAI answered 429, the paste escaped as a bare
    500 and the dashboard said "Network request failed." The owner must read what
    happened, nothing may be half-indexed, and the operator must be paged."""
    from app.knowledge import service
    from app.knowledge.embeddings import EmbeddingError

    class _DownEmbedder:
        dim = 1536
        model_name = "text-embedding-3-small"

        async def embed(self, texts):
            raise EmbeddingError("openai_error:RateLimitError")

    monkeypatch.setattr(service, "get_embedder", lambda *a, **k: _DownEmbedder())
    await _ready_account_with_luciel(client)
    r = await client.post(
        "/api/v1/admin/knowledge/sources/paste",
        json={"name": "Walk-ins policy", "text": "We welcome walk-ins on weekdays."},
    )
    assert r.status_code == 503
    body = r.json()
    assert "couldn't be indexed" in body["message"]
    assert body["retryAfterSeconds"] == 300
    listed = await client.get("/api/v1/admin/knowledge/sources")
    assert listed.status_code == 200
    assert [s for s in listed.json() if s["name"] == "Walk-ins policy"] == []
    metric_lines = [
        ln for ln in capsys.readouterr().out.splitlines() if '"KnowledgeRetrievalDegraded"' in ln
    ]
    assert metric_lines and "ingest:openai_error:RateLimitError" in metric_lines[-1]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_paste_ingest_then_list_and_chunks(client):
    await _ready_account_with_luciel(client)
    ri = await client.post(
        "/api/v1/admin/knowledge/sources/paste",
        json={"name": "FAQ", "text": "We serve small businesses. Starter engagement is $899."},
    )
    assert ri.status_code == 201
    src = ri.json()
    assert src["ingestionStatus"] == "ready"
    assert src["origin"] == "paste"
    source_id = src["sourceId"]

    rl = await client.get("/api/v1/admin/knowledge/sources")
    assert rl.status_code == 200
    assert any(s["sourceId"] == source_id for s in rl.json())

    rc = await client.get(f"/api/v1/admin/knowledge/sources/{source_id}/chunks")
    assert rc.status_code == 200
    page = rc.json()
    chunks = page["chunks"]
    assert len(chunks) >= 1
    assert "text" in chunks[0] and "ordinal" in chunks[0]
    # Round 7 WP-4c #6: the page carries the real count, not the page length.
    assert page["totalChunks"] == len(chunks) and page["pageSize"] == 20

    # quota now reflects the ingest
    rq = await client.get("/api/v1/admin/knowledge/quota")
    assert rq.json()["usedBytes"] > 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_used_by_questions_7d_reaches_the_wire_under_its_exact_key(client):
    """Live-caught 2026-08-23: the alias generator camelized `_7d` to `7D`, so
    the serializer's lowercase input key was silently DROPPED (the wire always
    said 0, under a key the frontend never read) and the delete confirmation
    claimed "no questions used this source" about a source analytics counted 6
    for. Pins the exact wire key AND a real traced count end-to-end."""
    from app.auth.tokens import decode_token
    from app.config import get_settings
    from app.db.session import tenant_session
    from app.models.instance import Instance
    from app.models.trace import RetrievalTraceEvent
    from sqlalchemy import select

    await _ready_account_with_luciel(client)
    ri = await client.post(
        "/api/v1/admin/knowledge/sources/paste",
        json={"name": "Hours", "text": "Open weekdays 8-6."},
    )
    source_id = uuid.UUID(ri.json()["sourceId"])
    admin_id = uuid.UUID(
        decode_token(client.cookies.get(get_settings().session_cookie_name))["sub"]
    )
    async with tenant_session(admin_id) as s:
        iid = (
            await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
        ).scalar_one()
        s.add(
            RetrievalTraceEvent(
                id=uuid.uuid4(),
                admin_id=admin_id,
                instance_id=iid,
                session_id=uuid.uuid4(),
                source_id=source_id,
            )
        )
        await s.flush()

    body = (await client.get("/api/v1/admin/knowledge/sources")).json()
    row = next(x for x in body if x["sourceId"] == str(source_id))
    assert row["usedByQuestions7d"] == 1  # the exact key the frontend reads
    assert "usedByQuestions7D" not in row  # the generator's casing never ships


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_delete_source_204(client):
    await _ready_account_with_luciel(client)
    ri = await client.post(
        "/api/v1/admin/knowledge/sources/paste",
        json={"name": "tmp", "text": "delete me content here"},
    )
    source_id = ri.json()["sourceId"]
    rd = await client.delete(f"/api/v1/admin/knowledge/sources/{source_id}")
    assert rd.status_code == 204
    rl = await client.get("/api/v1/admin/knowledge/sources")
    assert all(s["sourceId"] != source_id for s in rl.json())


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_delete_can_be_undone_through_the_api(client):
    """F109: POST .../restore inside the window brings the source back; a second
    restore (nothing to undo) is a 404 with owner-facing copy."""
    await _ready_account_with_luciel(client)
    ri = await client.post(
        "/api/v1/admin/knowledge/sources/paste",
        json={"name": "undo me", "text": "content the owner will want back"},
    )
    source_id = ri.json()["sourceId"]
    assert (await client.delete(f"/api/v1/admin/knowledge/sources/{source_id}")).status_code == 204
    rr = await client.post(f"/api/v1/admin/knowledge/sources/{source_id}/restore")
    assert rr.status_code == 200, rr.text
    assert rr.json()["sourceId"] == source_id
    rl = await client.get("/api/v1/admin/knowledge/sources")
    assert any(x["sourceId"] == source_id for x in rl.json())
    again = await client.post(f"/api/v1/admin/knowledge/sources/{source_id}/restore")
    assert again.status_code == 404
    assert "undo window" in again.text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_delete_missing_source_404(client):
    await _ready_account_with_luciel(client)
    rd = await client.delete(f"/api/v1/admin/knowledge/sources/{uuid.uuid4()}")
    assert rd.status_code == 404


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_restore_refuses_a_source_removed_at_its_origin(client):
    """Round 7 WP-4c #9: a sync-origin tombstone is not the owner's mis-click. The
    restore route says so — 409 naming the origin — never "not in its undo window",
    and never a revived page the origin no longer has."""
    from datetime import UTC, datetime

    from app.db.session import tenant_session
    from app.knowledge import service
    from app.models.knowledge import KnowledgeSource
    from sqlalchemy import select

    admin = await _ready_account_with_luciel(client)
    ri = await client.post(
        "/api/v1/admin/knowledge/sources/paste",
        json={"name": "Pricing page", "text": "a page the site has since removed"},
    )
    source_id = ri.json()["sourceId"]
    # The shape the sync leaves behind when the origin no longer has the item.
    async with tenant_session(admin) as s:
        src = (
            await s.execute(
                select(KnowledgeSource).where(KnowledgeSource.id == uuid.UUID(source_id))
            )
        ).scalar_one()
        src.source_origin = "website_crawl"
        src.soft_deleted_at = datetime.now(UTC)
        src.deleted_by = service.DELETED_BY_ORIGIN

    rr = await client.post(f"/api/v1/admin/knowledge/sources/{source_id}/restore")
    assert rr.status_code == 409, rr.text
    message = rr.json()["message"]
    assert "Pricing page was removed at its sync source (website_crawl)" in message
    assert "sync again" in message and "undo window" not in message
    # Nothing was revived: the list carries it only as the origin tombstone it is
    # (round 7 M10 lists those with ``deletedBy: "origin"`` so the owner reads "removed
    # at its source" without a failed restore), never as a live source.
    rl = await client.get("/api/v1/admin/knowledge/sources")
    listed = [x for x in rl.json() if x["sourceId"] == source_id]
    assert [x["deletedBy"] for x in listed] == ["origin"]
    async with tenant_session(admin) as s:
        src = (
            await s.execute(
                select(KnowledgeSource).where(KnowledgeSource.id == uuid.UUID(source_id))
            )
        ).scalar_one()
        assert src.soft_deleted_at is not None and src.deleted_by == service.DELETED_BY_ORIGIN
