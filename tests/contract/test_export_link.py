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
    # The presigned link is delivered in the API response (and by email) — the
    # channels the owner actually reads it from.
    url = r.json()["download_url"]
    assert "export.local" in url

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

    # Extract the key from the fake URL and read back the persisted bundle —
    # proves put_and_presign actually stored the bytes, not just minted a URL.
    key = url.split("https://export.local/", 1)[1].split("?expires=")[0]
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
