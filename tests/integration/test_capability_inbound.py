"""Capability-URL webhook lifecycle for tokenless Twilio tenants (C10/C11).

The one-click OAuth connect stores a bearer grant — no Account Auth Token — so
X-Twilio-Signature can never verify for those tenants (nor for API-key-only
form submissions, a pre-existing hole). Auto-config now mints a per-connection
256-bit ``cap`` parameter onto the registered URLs, and inbound verification
accepts EITHER a valid signature OR that token. Pinned here:

  - the token is minted once, stored in the SecretStore beside the credential,
    audited (``webhook_capability_provisioned``), and reused on re-config;
  - a tenant WITH an auth token gets no cap parameter — their URLs and
    verification stay byte-identical (the additive rule);
  - the route-level resolver hands verification both secrets, and the
    acceptance rule passes real capability inbounds while rejecting forgeries;
  - the voice status callback accepts a valid cap, 403s + audits a forged one
    (``inbound_capability_rejected``), and rate-limits per number.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels.phone_lifecycle import configure_number_webhooks
from app.channels.signatures import verify_twilio_inbound
from app.channels.tenant_twilio import (
    FIELD_CAPABILITY_TOKEN,
    WebhookUrls,
    ensure_capability_token,
)
from app.config import get_settings
from app.connections.secrets import get_secret_store, secret_ref_for
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

BASE = "https://api.vantagemind.test"


class RecordingConfigurator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, WebhookUrls]] = []

    async def configure(self, e164_number: str, urls: WebhookUrls) -> bool:
        self.calls.append((e164_number, urls))
        return True


async def _seed_tenant(admin: uuid.UUID) -> uuid.UUID:
    iid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin.hex[:8]}@x.com",
                email_lower=f"{admin.hex[:8]}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="Cap Co",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
    return iid


async def _seed_sms_connection(
    admin: uuid.UUID,
    iid: uuid.UUID,
    *,
    destination: str,
    material: dict[str, str],
) -> uuid.UUID:
    ref = secret_ref_for(admin, iid, "sms_sender")
    await get_secret_store().put(ref, dict(material))
    cid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=cid,
                admin_id=admin,
                instance_id=iid,
                connection_type="sms_sender",
                provider="twilio",
                auth_class="oauth_token" if "access_token" in material else "api_key",
                status="connected",
                secret_ref=ref,
                non_secret_config={"accountSid": "AC" + "5" * 32, "destination": destination},
            )
        )
        await s.flush()
    return cid


async def _audit_count(admin: uuid.UUID, event_type: str) -> int:
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == event_type,
                    )
                )
            )
            .scalars()
            .all()
        )
    return len(rows)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_oauth_material_gets_a_minted_audited_reusable_cap(monkeypatch):
    monkeypatch.setattr(get_settings(), "public_api_base_url", BASE, raising=False)
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    number = "+16045550170"
    await _seed_sms_connection(
        admin, iid, destination=number, material={"access_token": "bearer-tok"}
    )
    rec = RecordingConfigurator()
    async with tenant_session(admin) as s:
        conn = (
            await s.execute(select(InstanceConnection).where(InstanceConnection.admin_id == admin))
        ).scalar_one()
        await configure_number_webhooks(
            s,
            admin_id=admin,
            instance_id=iid,
            conn=conn,
            e164_number=number,
            webhook_config=rec,
        )
    _, urls = rec.calls[0]
    stored = await get_secret_store().get(secret_ref_for(admin, iid, "sms_sender")) or {}
    token = stored.get(FIELD_CAPABILITY_TOKEN)
    assert token, "a capability token must be minted for tokenless material"
    for url in (urls.voice_url, urls.sms_url, urls.status_callback_url):
        assert f"cap={token}" in url
    assert await _audit_count(admin, "webhook_capability_provisioned") == 1

    # Re-configuration reuses the token (stable URLs) and does not re-audit.
    async with tenant_session(admin) as s:
        conn = (
            await s.execute(select(InstanceConnection).where(InstanceConnection.admin_id == admin))
        ).scalar_one()
        again, minted = await ensure_capability_token(conn)
    assert again == token and minted is False
    assert await _audit_count(admin, "webhook_capability_provisioned") == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_auth_token_material_keeps_byte_identical_urls(monkeypatch):
    """The additive rule: a signature-covered tenant gets NO cap parameter."""
    monkeypatch.setattr(get_settings(), "public_api_base_url", BASE, raising=False)
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    number = "+16045550171"
    await _seed_sms_connection(admin, iid, destination=number, material={"authToken": "tok"})
    rec = RecordingConfigurator()
    async with tenant_session(admin) as s:
        conn = (
            await s.execute(select(InstanceConnection).where(InstanceConnection.admin_id == admin))
        ).scalar_one()
        await configure_number_webhooks(
            s,
            admin_id=admin,
            instance_id=iid,
            conn=conn,
            e164_number=number,
            webhook_config=rec,
        )
    _, urls = rec.calls[0]
    assert urls.sms_url == f"{BASE}/api/v1/webhooks/twilio/sms"
    assert "cap=" not in urls.voice_url
    assert await _audit_count(admin, "webhook_capability_provisioned") == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_route_resolver_hands_verification_both_secrets():
    """_tenant_twilio_inbound_auth resolves (auth_token, capability) by the To
    number, and the acceptance rule admits the registered URL while rejecting a
    forged cap — the destination lookup IS the tenant binding."""
    from app.api.v1.webhooks.channels import _tenant_twilio_inbound_auth

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    number = "+16045550172"
    await _seed_sms_connection(
        admin,
        iid,
        destination=number,
        material={"access_token": "bearer-tok", FIELD_CAPABILITY_TOKEN: "cap-secret-value"},
    )
    auth_token, capability = await _tenant_twilio_inbound_auth(number)
    assert auth_token is None
    assert capability == "cap-secret-value"

    params = {"To": number, "Body": "hi", "From": "+16045550001"}
    good = f"{BASE}/api/v1/webhooks/twilio/sms?cap=cap-secret-value"
    forged = f"{BASE}/api/v1/webhooks/twilio/sms?cap=cap-guess"
    assert verify_twilio_inbound("platform", good, params, "", capability_token=capability)
    assert not verify_twilio_inbound("platform", forged, params, "", capability_token=capability)


# The HTTP-level status-callback contract (valid cap → 200, forged cap → 403 +
# audit, per-number 429) lives in tests/contract/test_capability_status_callback.py
# — the shared `client` fixture is contract-scoped.
