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

    r = await client.post("/api/v1/admin/account/close", json={"confirmDeleteLuciel": True})
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
async def test_the_export_response_reports_the_email_outcome(client, monkeypatch):
    """Round 7 WP-5 item 4: the dispatcher's return value used to be dropped, so the
    UI said "check your inbox" over a mail that never left. Mock/CI: the Null sender
    records it; a sender that throws is reported as failed:<class> and the link is
    still in the body — a mail failure never loses the export."""
    from app.notifications import email_dispatcher

    LocalExportStore.reset()
    await _signup_verify_login(client)
    r = await client.post("/api/v1/admin/account/export")
    assert r.status_code == 200
    assert r.json()["emailOutcome"] == "recorded"

    class Broken:
        async def send(self, **kwargs):
            raise RuntimeError("ses down")

    monkeypatch.setattr(email_dispatcher, "build_email_sender", lambda: Broken())
    r = await client.post("/api/v1/admin/account/export")
    assert r.status_code == 200, "a mail failure never loses the export"
    assert r.json()["emailOutcome"] == "failed:RuntimeError"
    assert "/api/v1/exports/" in r.json()["download_url"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_export_link_is_throttled_per_link(client):
    """Adversarial review R-WP2-2: the download door is unauthenticated by design, so
    a leaked or replayed link must not become an unbounded audit-row and presign
    mill. The burst covers a browser that retries; a loop gets 429s."""
    from app.lifecycle.export_link import mint_export_token
    from app.lifecycle.export_store import export_key_for

    LocalExportStore.reset()
    admin_id = await _signup_verify_login(client)
    export_id = uuid.uuid4()
    await LocalExportStore().put(key=export_key_for(admin_id, export_id), data=b"zip")
    token = mint_export_token(admin_id, export_id, ttl_days=7)
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
async def test_a_valid_link_to_a_swept_bundle_is_a_410_not_a_redirect_to_nothing(client):
    """Round 7 WP-5 item 8: a bundle can be gone while the account remains (removed
    by hand, or the bucket's lifecycle backstop), so the 7-day token still verifies.
    (A Luciel hard delete no longer sweeps bundles — money review finding 1.) The
    presign is computed locally and "succeeded" for the missing object — the owner
    was redirected to the
    bucket's raw NoSuchKey page. The door now asks the store first; a store that
    cannot answer is NOT read as gone (the redirect goes ahead, as before)."""
    from app.lifecycle.export_link import mint_export_token
    from app.lifecycle.export_store import export_key_for, export_prefix_for, set_export_store

    LocalExportStore.reset()
    admin_id = await _signup_verify_login(client)
    export_id = uuid.uuid4()
    store = LocalExportStore()
    await store.put(key=export_key_for(admin_id, export_id), data=b"zip")
    token = mint_export_token(admin_id, export_id, ttl_days=7)
    assert (await client.get(f"/api/v1/exports/{token}", follow_redirects=False)).status_code == 302

    await store.delete_prefix(prefix=export_prefix_for(admin_id))  # what the cascade does
    gone = await client.get(f"/api/v1/exports/{token}", follow_redirects=False)
    assert gone.status_code == 410
    assert "expired" in gone.text
    async with tenant_session(admin_id) as s:
        downloads = (
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
    assert len(downloads) == 1, "a refused visit is not recorded as a download"

    class Unreachable(LocalExportStore):
        async def exists(self, *, key: str) -> bool:
            raise TimeoutError("head timed out")

    set_export_store(Unreachable())
    try:
        unknown = await client.get(f"/api/v1/exports/{token}", follow_redirects=False)
    finally:
        set_export_store(None)
    assert unknown.status_code == 302, "an unanswered existence check is not 'gone'"


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


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_luciel_hard_delete_does_not_kill_an_export_link_the_open_account_holds(client):
    """Round 7 money review, finding 1. Live condition: the owner deletes the Luciel on
    day 0 and keeps the account; on day 28 they download their data (200, ttlDays 7,
    the email says the link is valid 7 days); on day 30 the grace-expiry sweep runs the
    Luciel-scope hard delete. That cascade swept every bundle under the tenant's
    prefix, so on day 31 the link answered 410 "expired" with four promised days left.
    Only the account purge sweeps bundles now; the account here is still open."""
    from datetime import UTC, datetime, timedelta

    from app.lifecycle import grace_expiry
    from app.models.instance import Instance, LucielState
    from sqlalchemy import update

    LocalExportStore.reset()
    admin_id = await _signup_verify_login(client)
    created = await client.post(
        "/api/v1/admin/luciel",
        json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"},
    )
    assert created.status_code == 200, created.text
    deleted = await client.post("/api/v1/admin/luciel/delete")
    assert deleted.status_code == 200, deleted.text
    # Day 28 of the grace window.
    day0 = datetime.now(UTC) - timedelta(days=28)
    async with tenant_session(admin_id) as s:
        await s.execute(
            update(Instance)
            .where(Instance.admin_id == admin_id)
            .values(grace_window_started_at=day0, soft_deleted_at=day0)
        )

    r = await client.post("/api/v1/admin/account/export")
    assert r.status_code == 200, r.text
    assert r.json()["ttl_days"] == 7
    door = r.json()["download_url"].split("/api/v1", 1)[1]

    # Day 30: the sweep destroys the Luciel's data.
    async with tenant_session(admin_id) as s:
        counts = await grace_expiry.expire_grace_window_if_due(
            s, admin_id=admin_id, now=day0 + timedelta(days=30, minutes=1)
        )
    assert counts is not None, "the grace window expired and the cascade ran"
    async with tenant_session(admin_id) as s:
        inst = (await s.execute(select(Instance))).scalar_one_or_none()
    assert inst is None or inst.state == LucielState.luciel_hard_deleted

    # The link the owner was promised for seven days still opens.
    opened = await client.get(f"/api/v1{door}", follow_redirects=False)
    assert opened.status_code == 302, opened.text
