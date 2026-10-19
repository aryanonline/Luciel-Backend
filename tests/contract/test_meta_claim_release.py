"""Disconnecting a Meta grant releases its routing claims (round 7 M10).

Live gap: the owner ruling of 2026-09-14 (round 7 WP-4a item 12) — a disconnected
channel connection RELEASES its routing claim, ownership is re-proven on reconnect —
was applied to the SMS number and the mailbox only. A disconnected Meta grant kept
its ``destinations`` map, and the destination resolver admits every non-revoked row,
so the WhatsApp phone-number id / Messenger Page id / Instagram account id:
  * kept resolving inbound to the account that disconnected it, and
  * kept refusing its next legitimate holder as "already connected to another
    account" — forever.

Now: the disconnect moves the ids to ``released_destinations`` (not a claim) and
audits them like the SMS/mailbox release; another account can bind them; and a
reconnect by the first account re-proves each id against the NEW grant and
re-claims it — or parks it under ``pending_destinations`` (never routable) when it
was taken meanwhile or cannot be proven, saying so on the row.
"""

from __future__ import annotations

import uuid

import pytest
from app.api.v1.admin.connections import META_UNPROVEN_DETAIL
from app.channels import routing
from app.connections import meta_assets, service
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select

from tests.contract.test_connection_lifecycle import _admin_id, _connected_meta

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

PASSWORD = "pw-123456"


async def _signup_with_luciel(client) -> tuple[str, uuid.UUID]:
    from app.auth import access

    email = f"meta-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": PASSWORD, "captchaToken": "t"}
    )
    _a, token = await access.signup(email, PASSWORD)
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    await client.post(
        "/api/v1/admin/luciel", json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"}
    )
    return email, _admin_id(client)


async def _login(client, email: str) -> None:
    client.cookies.clear()
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text


def _grant_covers(monkeypatch, assets: set[str]) -> None:
    """The live asset lister, as Meta would answer for a grant managing ``assets``."""

    async def _lister(*, material, connection_type, transport):
        return set(assets)

    monkeypatch.setattr(meta_assets, "lister_for_posture", lambda: _lister)


async def _holder(admin_id: uuid.UUID, asset_id: str) -> uuid.UUID | None:
    async with tenant_session(admin_id) as s:
        return await routing.owner_of_meta_destination(s, asset_id)


async def _row(admin_id: uuid.UUID, cid: str) -> InstanceConnection:
    async with tenant_session(admin_id) as s:
        return (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == uuid.UUID(cid))
            )
        ).scalar_one()


async def _audits(admin_id: uuid.UUID, event_type: str) -> list[dict]:
    async with tenant_session(admin_id) as s:
        rows = (
            await s.execute(
                select(AdminAuditLog)
                .where(AdminAuditLog.admin_id == admin_id, AdminAuditLog.event_type == event_type)
                .order_by(AdminAuditLog.seq.asc())
            )
        ).scalars()
        return [dict(e.event_payload or {}) for e in rows]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_meta_disconnect_releases_rebind_by_another_account_and_reconnect_re_proves(
    client, monkeypatch
):
    pnid = f"pnid-{uuid.uuid4().hex[:10]}"
    page = f"page-{uuid.uuid4().hex[:10]}"

    # --- Account A binds WhatsApp + Messenger on its Facebook grant. -----------------
    email_a, admin_a = await _signup_with_luciel(client)
    _grant_covers(monkeypatch, {pnid, page})
    cid_a = await _connected_meta(client, monkeypatch)
    path_a = f"/api/v1/admin/connections/{cid_a}/destination"
    for channel, asset_id in (("whatsapp", pnid), ("messenger", page)):
        r = await client.put(path_a, json={"destination": asset_id, "channel": channel})
        assert r.status_code == 200, r.text
    assert await _holder(admin_a, pnid) == admin_a
    assert await _holder(admin_a, page) == admin_a

    # --- A disconnects: both claims are released, kept on file only as a copy. -------
    out = await client.post(f"/api/v1/admin/connections/{cid_a}/disconnect")
    assert out.status_code == 200, out.text
    cfg = (await _row(admin_a, cid_a)).non_secret_config or {}
    assert "destinations" not in cfg and "pending_destinations" not in cfg
    assert cfg[service.RELEASED_DESTINATIONS_KEY] == {"whatsapp": pnid, "messenger": page}
    assert await _holder(admin_a, pnid) is None, "a disconnected grant must not route inbound"
    assert await _holder(admin_a, page) is None
    (disconnect,) = [
        p
        for p in await _audits(admin_a, "connection_status_changed")
        if p.get("reason") == "disconnect"
    ]
    assert disconnect["routing_claim_released"] is True
    assert disconnect["released_destinations"] == {"whatsapp": pnid, "messenger": page}

    # --- Account B can now bind A's former WhatsApp number. --------------------------
    _email_b, admin_b = await _signup_with_luciel(client)
    assert admin_b != admin_a
    _grant_covers(monkeypatch, {pnid})
    cid_b = await _connected_meta(client, monkeypatch)
    taken = await client.put(
        f"/api/v1/admin/connections/{cid_b}/destination",
        json={"destination": pnid, "channel": "whatsapp"},
    )
    assert taken.status_code == 200, taken.text
    assert await _holder(admin_b, pnid) == admin_b

    # --- A reconnects: the free Page is re-proven and re-claimed; the number B now ---
    # --- holds is parked (never routable) and the row says why. ----------------------
    await _login(client, email_a)
    _grant_covers(monkeypatch, {pnid, page})
    assert await _connected_meta(client, monkeypatch) == cid_a  # the same row comes back
    row = await _row(admin_a, cid_a)
    cfg = row.non_secret_config or {}
    assert row.status == "connected"
    assert cfg.get("destinations") == {"messenger": page}
    assert cfg.get("pending_destinations") == {"whatsapp": pnid}
    assert service.RELEASED_DESTINATIONS_KEY not in cfg, "the released copy is consumed"
    assert service.RELEASED_DESTINATION_KEY not in cfg
    assert "WhatsApp number was connected to another account" in (row.status_detail or "")
    assert await _holder(admin_a, page) == admin_a
    assert await _holder(admin_a, pnid) == admin_b, "B's claim is untouched"

    reclaims = {
        p["channel"]: p["outcome"]
        for p in await _audits(admin_a, "instance_configured")
        if p.get("action") == "reclaim_destination"
    }
    assert reclaims == {"whatsapp": service.PARKED_TAKEN, "messenger": service.RECLAIMED}
    rejected = await _audits(admin_a, "destination_claim_rejected")
    assert rejected == [{"connection_type": "channel_auth", "destination_suffix": pnid[-4:]}]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_reconnect_that_cannot_prove_the_ids_parks_them(client, monkeypatch):
    """The new grant does not cover the old Page (or Meta cannot be asked): the id is
    parked exactly as an unproven bind is — not routable, honest notice — and the
    owner re-binds once Meta confirms it."""
    page = f"page-{uuid.uuid4().hex[:10]}"
    _email, admin = await _signup_with_luciel(client)
    _grant_covers(monkeypatch, {page})
    cid = await _connected_meta(client, monkeypatch)
    r = await client.put(
        f"/api/v1/admin/connections/{cid}/destination",
        json={"destination": page, "channel": "messenger"},
    )
    assert r.status_code == 200, r.text
    assert (await client.post(f"/api/v1/admin/connections/{cid}/disconnect")).status_code == 200

    _grant_covers(monkeypatch, set())  # a different Facebook user re-authorized
    await _connected_meta(client, monkeypatch)
    row = await _row(admin, cid)
    cfg = row.non_secret_config or {}
    assert not cfg.get("destinations")
    assert cfg.get("pending_destinations") == {"messenger": page}
    assert row.status_detail == META_UNPROVEN_DETAIL
    assert await _holder(admin, page) is None


async def _seed_tenant() -> tuple[uuid.UUID, uuid.UUID]:
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@x.com",
                email_lower=f"{admin}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="L",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
    return admin, iid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_instagram_disconnect_releases_the_account_id_and_a_legacy_scalar_too():
    ig = f"ig-{uuid.uuid4().hex[:10]}"
    legacy = f"page-{uuid.uuid4().hex[:10]}"
    admin, iid = await _seed_tenant()
    ig_cid, fb_cid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=ig_cid,
                admin_id=admin,
                instance_id=iid,
                connection_type="instagram_auth",
                auth_class="oauth_token",
                provider="instagram",
                status="connected",
                non_secret_config={"destinations": {"instagram": ig}},
            )
        )
        s.add(
            InstanceConnection(
                connection_id=fb_cid,
                admin_id=admin,
                instance_id=iid,
                connection_type="channel_auth",
                auth_class="oauth_token",
                provider="meta",
                status="connected",
                # A row bound before per-channel destinations existed.
                non_secret_config={"destination": legacy},
            )
        )
    assert await _holder(admin, ig) == admin
    assert await _holder(admin, legacy) == admin

    async with tenant_session(admin) as s:
        await service.disconnect(s, admin_id=admin, connection_id=ig_cid, user_id=None)
        await service.disconnect(s, admin_id=admin, connection_id=fb_cid, user_id=None)

    ig_cfg = (await _row(admin, str(ig_cid))).non_secret_config or {}
    assert ig_cfg == {service.RELEASED_DESTINATIONS_KEY: {"instagram": ig}}
    fb_cfg = (await _row(admin, str(fb_cid))).non_secret_config or {}
    assert fb_cfg == {service.RELEASED_DESTINATION_KEY: legacy}
    assert await _holder(admin, ig) is None
    assert await _holder(admin, legacy) is None
    released = [
        p
        for p in await _audits(admin, "connection_status_changed")
        if p.get("reason") == "disconnect"
    ]
    assert [p.get("released_destinations") for p in released] == [{"instagram": ig}, None]
    assert [p.get("released_destination") for p in released] == [None, legacy]
    assert all(p["routing_claim_released"] is True for p in released)
