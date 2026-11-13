"""Contract conformance for /api/v1/admin/luciel* lifecycle + the 'fresh' and
'paused'/'grace' states (matching the frozen mock scenarios).

Reproduces the wire behavior the frontend mock models:
  - fresh (verified, no Luciel) -> luciel.get() returns JSON null (200)
  - create -> Luciel active, camelCase shape
  - pause -> state 'paused'; resume -> 'active'
  - delete -> 'luciel_grace_window' + graceWindowStartedAt set
  - restore -> 'active' (NOT paused), graceWindowStartedAt cleared
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.contract]


async def _signup_verify_login(client) -> str:
    from app.auth import access

    email = f"life-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    _acct, token = await access.signup(email, "pw-123456")  # reissue + fresh verify token
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return email


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_fresh_account_get_luciel_returns_null(client):
    await _signup_verify_login(client)
    r = await client.get("/api/v1/admin/luciel")
    assert r.status_code == 200
    assert r.json() is None  # contract: JSON null, not 404


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_create_then_get_returns_active_luciel_camelcase(client):
    await _signup_verify_login(client)
    rc = await client.post(
        "/api/v1/admin/luciel",
        json={
            "name": "Sarah's Front-Desk Assistant",
            "websiteUrl": "sarahchen.com",
            "businessOneLiner": "Consultant serving the GTA.",
        },
    )
    assert rc.status_code == 200
    body = rc.json()
    assert body["state"] == "active"
    assert body["name"] == "Sarah's Front-Desk Assistant"
    assert "instanceId" in body and "embedKeyPublicId" in body
    assert isinstance(body["channels"], list) and isinstance(body["tools"], list)
    # me() now routes to dashboard (has Luciel).
    rme = await client.get("/api/v1/auth/me")
    assert rme.json()["nextRoute"] == "dashboard"
    assert rme.json()["account"]["hasLuciel"] is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_pause_resume(client):
    await _signup_verify_login(client)
    await client.post(
        "/api/v1/admin/luciel",
        json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"},
    )
    rp = await client.post("/api/v1/admin/luciel/pause")
    assert rp.json()["state"] == "paused"
    rr = await client.post("/api/v1/admin/luciel/resume")
    assert rr.json()["state"] == "active"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_delete_to_grace_then_restore_active(client):
    await _signup_verify_login(client)
    await client.post(
        "/api/v1/admin/luciel",
        json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"},
    )
    rd = await client.post("/api/v1/admin/luciel/delete")
    assert rd.json()["state"] == "luciel_grace_window"
    assert rd.json()["graceWindowStartedAt"] is not None  # grace stamp set

    rr = await client.post("/api/v1/admin/luciel/restore")
    assert rr.json()["state"] == "active"  # restore → active, NOT paused
    assert rr.json()["graceWindowStartedAt"] is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_create_twice_conflicts(client):
    await _signup_verify_login(client)
    body = {"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"}
    await client.post("/api/v1/admin/luciel", json=body)
    r2 = await client.post("/api/v1/admin/luciel", json=body)
    assert r2.status_code == 409  # conflict


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_update_pillars(client):
    await _signup_verify_login(client)
    await client.post(
        "/api/v1/admin/luciel",
        json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"},
    )
    # personality update round-trips
    rp = await client.put(
        "/api/v1/admin/luciel/personality",
        json={"preset": "professional_advisor", "businessContext": "Markham SMBs"},
    )
    assert rp.json()["personality"]["preset"] == "professional_advisor"
    # escalation update
    re = await client.put(
        "/api/v1/admin/luciel/escalation",
        json={"primaryEmail": "sarah@x.com", "preferredChannel": "email"},
    )
    assert re.json()["escalation"]["primaryEmail"] == "sarah@x.com"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_account_export_and_close(client):
    await _signup_verify_login(client)
    await client.post(
        "/api/v1/admin/luciel",
        json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"},
    )
    rex = await client.post("/api/v1/admin/account/export")
    assert rex.status_code == 200 and rex.json()["ok"] is True
    rc = await client.post("/api/v1/admin/account/close", json={"confirmDeleteLuciel": True})
    assert rc.status_code == 204
