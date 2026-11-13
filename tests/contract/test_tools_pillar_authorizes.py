"""Enabling a tool in the dashboard authorizes it for the broker (Arch §3.3.3 gate 1).

The tools pillar stores its checklist as JSONB on the instance, but the broker's
default-deny gate reads the relational ``instance_tool_authorizations`` table. Nothing
wrote that table, so a tool the admin had switched ON was still refused
``not_authorized`` at dispatch. These tests cover the PUT → table path, including that
the two representations stay consistent.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.tool_auth import InstanceToolAuthorization
from sqlalchemy import select

pytestmark = [pytest.mark.contract]


async def _signup_and_create(client) -> uuid.UUID:
    """Log a fresh admin in, create their Luciel, and return the admin_id."""
    from app.auth import access

    email = f"tools-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    await client.post(
        "/api/v1/admin/luciel",
        json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"},
    )
    return acct.admin_id


async def _authorizations(admin: uuid.UUID) -> dict[str, bool]:
    async with tenant_session(admin) as s:
        return {
            r.tool_id: r.enabled
            for r in (
                await s.execute(
                    select(InstanceToolAuthorization).where(
                        InstanceToolAuthorization.admin_id == admin
                    )
                )
            ).scalars()
        }


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_enabling_a_tool_writes_its_authorization_row(client) -> None:
    admin = await _signup_and_create(client)
    assert await _authorizations(admin) == {}  # default-deny: no rows until asked

    r = await client.put(
        "/api/v1/admin/luciel/tools",
        json={
            "tools": [
                {"id": "send_email", "enabled": True},
                {"id": "schedule_callback", "enabled": False},
            ]
        },
    )
    assert r.status_code == 200
    assert await _authorizations(admin) == {"send_email": True, "schedule_callback": False}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_disabling_a_tool_flips_the_row_and_keeps_it(client) -> None:
    """Vision §3.7.1: disabling is reversible, so the row and its history survive."""
    admin = await _signup_and_create(client)
    await client.put(
        "/api/v1/admin/luciel/tools", json={"tools": [{"id": "send_sms", "enabled": True}]}
    )
    await client.put(
        "/api/v1/admin/luciel/tools", json={"tools": [{"id": "send_sms", "enabled": False}]}
    )
    assert await _authorizations(admin) == {"send_sms": False}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_jsonb_pillar_and_the_gate_table_agree(client) -> None:
    """Both are written in one transaction, so the dashboard read-back can never claim a
    tool is on while gate 1 refuses it."""
    admin = await _signup_and_create(client)
    r = await client.put(
        "/api/v1/admin/luciel/tools",
        json={
            "tools": [
                {"id": "push_to_crm", "enabled": True},
                {"id": "lookup_record", "enabled": False},
            ]
        },
    )
    assert r.status_code == 200
    from_pillar = {t["id"]: t["enabled"] for t in r.json()["tools"]}
    assert from_pillar == await _authorizations(admin)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_rejected_body_writes_no_authorization(client) -> None:
    """The write is inside the request transaction: an invalid pillar body must leave
    gate 1 untouched rather than half-authorizing the valid entries."""
    admin = await _signup_and_create(client)
    r = await client.put(
        "/api/v1/admin/luciel/tools",
        json={
            "tools": [{"id": "send_email", "enabled": True}, {"id": "escalate", "enabled": True}]
        },
    )
    assert r.status_code == 422  # cognition is never an addon tool (§3.8.7)
    assert await _authorizations(admin) == {}
