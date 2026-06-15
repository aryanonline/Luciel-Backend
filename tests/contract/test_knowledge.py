"""Contract conformance for /api/v1/admin/knowledge/* (frozen + additive ingest)."""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _ready_account_with_luciel(client) -> None:
    from app.auth import access

    email = f"kb-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    _a, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    await client.post(
        "/api/v1/admin/luciel", json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"}
    )


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
    chunks = rc.json()
    assert len(chunks) >= 1
    assert "text" in chunks[0] and "ordinal" in chunks[0]

    # quota now reflects the ingest
    rq = await client.get("/api/v1/admin/knowledge/quota")
    assert rq.json()["usedBytes"] > 0


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
async def test_delete_missing_source_404(client):
    await _ready_account_with_luciel(client)
    rd = await client.delete(f"/api/v1/admin/knowledge/sources/{uuid.uuid4()}")
    assert rd.status_code == 404
