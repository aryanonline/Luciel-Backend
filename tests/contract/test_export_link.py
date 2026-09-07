"""Export link is now persisted + presigned (Architecture §3.6.6 / §5.10) —
previously ``request_export``/``close_account`` built the ZIP and discarded it.

Uses the injected :class:`LocalExportStore` fake (mock mode) — NO network call.
Asserts the ``data_export_ready`` audit payload carries a 7-day-TTL download
link AND that the persisted bytes are a real ZIP with the documented entries.
"""

from __future__ import annotations

import io
import uuid
import zipfile

import pytest
from app.db.session import tenant_session
from app.lifecycle.export_store import LocalExportStore
from app.models.audit import AdminAuditLog
from sqlalchemy import select

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _signup_verify_login(client) -> uuid.UUID:
    from app.auth import access

    email = f"export-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return acct.id


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_request_export_uploads_and_issues_presigned_link(client):
    LocalExportStore.reset()
    admin_id = await _signup_verify_login(client)
    await client.post(
        "/api/v1/admin/luciel",
        json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"},
    )

    r = await client.post("/api/v1/admin/account/export")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # The link is delivered in the API response (and by email) — the channels the
    # owner actually reads it from. It is an API door with a signed 7-day token
    # (2026-09-05 audit F028), not a presign that dies with the task-role
    # credentials within hours; opening it mints a fresh short presign.
    url = r.json()["download_url"]
    assert "/api/v1/exports/" in url
    assert "export.local" not in url
    door = url.split("/api/v1", 1)[1]
    opened = await client.get(f"/api/v1{door}", follow_redirects=False)
    assert opened.status_code == 302, opened.text
    presigned = opened.headers["location"]
    assert "export.local" in presigned

    # The data_export_ready audit event records THAT an export was minted (id, size,
    # TTL) — and deliberately NOT the presigned link: the audit log is append-only,
    # 1-year retained, and included in every later export bundle, so a persisted URL
    # would be a working door to a full tenant data dump long past its 7-day life.
    async with tenant_session(admin_id) as s:
        rows = (
            await s.execute(
                select(AdminAuditLog.event_type, AdminAuditLog.event_payload).where(
                    AdminAuditLog.event_type == "data_export_ready"
                )
            )
        ).all()
    assert len(rows) == 1
    payload = rows[0].event_payload
    assert payload["ttl_days"] == 7
    assert payload["size_bytes"] > 0
    assert payload["export_id"]
    assert "download_url" not in payload

    # Extract the key from the presign and read back the persisted bundle —
    # proves the export actually stored the bytes, not just minted a URL.
    key = presigned.split("https://export.local/", 1)[1].split("?expires=")[0]
    zip_bytes = LocalExportStore.get_bytes(key)
    assert zip_bytes is not None

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = set(z.namelist())
        assert "README.txt" in names
        assert "leads.json" in names
        assert "leads.csv" in names
        assert "instance.json" in names
        assert "audit_log.jsonl" in names
        assert "conversations/conversations.csv" in names
        # Never leak secret material into the export (§3.8.3).
        instance_json = z.read("instance.json").decode()
        assert "secret_ref" not in instance_json


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_close_account_also_uploads_export_with_context(client):
    LocalExportStore.reset()
    admin_id = await _signup_verify_login(client)
    await client.post(
        "/api/v1/admin/luciel",
        json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"},
    )

    r = await client.post("/api/v1/admin/account/close")
    assert r.status_code == 204

    async with tenant_session(admin_id) as s:
        rows = (
            await s.execute(
                select(AdminAuditLog.event_payload).where(
                    AdminAuditLog.event_type == "data_export_ready"
                )
            )
        ).all()
    assert len(rows) == 1
    payload = rows[0].event_payload
    assert payload["context"] == "account_close"
    assert payload["ttl_days"] == 7


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_export_link_is_throttled_per_link(client):
    """Adversarial review R-WP2-2: the download door is unauthenticated by design, so
    a leaked or replayed link must not become an unbounded audit-row and presign
    mill. The burst covers a browser that retries; a loop gets 429s."""
    from app.lifecycle.export_link import mint_export_token

    LocalExportStore.reset()
    admin_id = await _signup_verify_login(client)
    token = mint_export_token(admin_id, uuid.uuid4(), ttl_days=7)
    statuses = [
        (await client.get(f"/api/v1/exports/{token}", follow_redirects=False)).status_code
        for _ in range(8)
    ]
    assert statuses[:5] == [302] * 5, statuses
    assert set(statuses[5:]) == {429}, statuses
    async with tenant_session(admin_id) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin_id,
                        AdminAuditLog.event_type == "data_export_downloaded",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 5, "a throttled visit writes no audit row"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_bad_or_expired_export_link_is_a_plain_410(client):
    from app.auth.tokens import mint_link_token

    bad = await client.get("/api/v1/exports/not-a-token", follow_redirects=False)
    assert bad.status_code == 410
    assert "expired" in bad.text
    # A token of the wrong purpose never opens an export, however valid its signature.
    verify_token = mint_link_token(uuid.uuid4(), "verify", 600, "jti")
    wrong = await client.get(f"/api/v1/exports/{verify_token}", follow_redirects=False)
    assert wrong.status_code == 410
    # An expired export token is refused the same way.
    stale = mint_link_token(uuid.uuid4(), "export", -1, str(uuid.uuid4()))
    assert (await client.get(f"/api/v1/exports/{stale}", follow_redirects=False)).status_code == 410


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_bundle_contains_the_original_uploaded_files(client):
    """§5.10 / the bundle README: "your original uploaded files" (audit F016) — the
    upload's bytes come back as sent, and a pasted source comes back as its text."""
    from app.knowledge.original_store import LocalOriginalStore, set_original_store

    LocalExportStore.reset()
    LocalOriginalStore.reset()
    set_original_store(LocalOriginalStore())
    try:
        await _signup_verify_login(client)
        await client.post(
            "/api/v1/admin/luciel",
            json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"},
        )
        original = b"Return policy: 30 days, receipt required.\nExchanges welcome."
        up = await client.post(
            "/api/v1/admin/knowledge/sources/upload",
            files={"file": ("Return policy.txt", original, "text/plain")},
            data={"name": "Return policy.txt"},
        )
        assert up.status_code == 201, up.text
        pasted = await client.post(
            "/api/v1/admin/knowledge/sources/paste",
            json={"name": "Hours", "text": "Open 9 to 5 weekdays, closed Sunday."},
        )
        assert pasted.status_code == 201, pasted.text

        r = await client.post("/api/v1/admin/account/export")
        assert r.status_code == 200
        door = r.json()["download_url"].split("/api/v1", 1)[1]
        presigned = (await client.get(f"/api/v1{door}", follow_redirects=False)).headers["location"]
        key = presigned.split("https://export.local/", 1)[1].split("?expires=")[0]
        with zipfile.ZipFile(io.BytesIO(LocalExportStore.get_bytes(key))) as z:
            names = set(z.namelist())
            assert "knowledge/originals/Return policy.txt" in names, sorted(names)
            assert z.read("knowledge/originals/Return policy.txt") == original
            assert "knowledge/text/Hours.txt" in names, sorted(names)
            assert b"Open 9 to 5" in z.read("knowledge/text/Hours.txt")
            manifest = {
                m["name"]: m for m in __import__("json").loads(z.read("knowledge/manifest.json"))
            }
            assert manifest["Return policy.txt"]["file"] == "knowledge/originals/Return policy.txt"
            assert manifest["Hours"]["file"] == "knowledge/text/Hours.txt"
    finally:
        set_original_store(None)
        LocalOriginalStore.reset()
