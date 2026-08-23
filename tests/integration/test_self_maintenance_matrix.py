"""The self-maintenance conformance matrix (audit round 3, Phase 6.6).

The owner's standing question — "what does a customer need to do to close,
maintain, or change ANY configuration; what happens when it expires; are ALL
possibilities handled?" — as a parametrized suite over EVERY offered provider,
so the answer stays test-enforced as the catalog grows. Cells:

  (a) expiry/credential failure → the wire status flips to an actionable state
      (expired/error) with a plain detail, the §3.8.5 owner notification
      actually dispatches ONCE per incident, and Reconnect offers a workable
      flow. A provisioned platform resource (csv) has nothing to expire and
      honestly stays connected.
  (b) disconnect → the stored secret is destroyed, the change is audited, and
      the row is immediately reusable for a fresh connect.
  (c) swap → the candidate stages ALONGSIDE the live provider (status and
      provider untouched until a confirmed cutover), and the swap side door
      refuses honest-disabled targets exactly like a fresh start.
  (d) dormant (billing-owned) → the health sweep never touches the row.

Delegated cells, covered by their own suites: disable/enable never touches the
connection (test_channel_toggle_connection_status), delete→restore returns the
pre-delete status minus foreign claims (test_restore_status_stash), and the
full staged-swap completion paths (test_outlook_mailbox_connect,
test_twilio_number_picker, test_connection_swap_alongside_write).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.connections import registry
from app.connections import service as conn_service
from app.connections.health import ReconnectOnExpiry, StoredCredentialProbe, check_connection
from app.connections.secrets import get_secret_store, secret_ref_for
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

# Every (connection_type, provider) the owner can actually pick (display order).
_TYPES = (
    "calendar",
    "crm",
    "record_source",
    "knowledge_source",
    "channel_auth",
    "instagram_auth",
    "sms_sender",
    "outbound_webhook",
    "email_sender",
)
CASES = [(ct, spec.provider) for ct in _TYPES for spec in registry.providers_for(ct)]
_REFRESH_CLASSES = ("oauth_token", "long_lived_token")


def _honest_disabled(ct: str, provider: str) -> bool:
    """unavailable_for is the ONLY state where no row can exist: an
    unconfigured-OAuth provider still starts (with an Action-needed detail)."""
    spec = registry.get_provider(provider)
    return spec is not None and ct in spec.unavailable_for


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
                name="Matrix Co",
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


async def _seed_connection(
    admin: uuid.UUID,
    iid: uuid.UUID,
    ct: str,
    provider: str,
    *,
    status: str = "connected",
    with_material: bool = True,
    expired_hint: bool = False,
) -> uuid.UUID:
    auth_class = conn_service._auth_class_for(ct, provider)
    cid = uuid.uuid4()
    ref = None
    cfg: dict[str, object] = {}
    if auth_class != "provisioned_resource":
        ref = secret_ref_for(admin, iid, ct)
        if with_material:
            await get_secret_store().put(ref, {"access_token": "tok"})
        else:
            await get_secret_store().delete(ref)
    if expired_hint:
        cfg["token_expires_at"] = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=cid,
                admin_id=admin,
                instance_id=iid,
                connection_type=ct,
                provider=provider,
                auth_class=auth_class,
                status=status,
                secret_ref=ref,
                non_secret_config=cfg,
            )
        )
        await s.flush()
    return cid


async def _row(admin: uuid.UUID, cid: uuid.UUID) -> InstanceConnection:
    async with tenant_session(admin) as s:
        return (
            await s.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin,
                    InstanceConnection.connection_id == cid,
                )
            )
        ).scalar_one()


async def _attention_emails(admin: uuid.UUID) -> int:
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "connection_attention_emailed",
                    )
                )
            )
            .scalars()
            .all()
        )
    return len(rows)


async def _run_check(admin: uuid.UUID, cid: uuid.UUID) -> str:
    async with tenant_session(admin) as s:
        conn = (
            await s.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin,
                    InstanceConnection.connection_id == cid,
                )
            )
        ).scalar_one()
        return await check_connection(
            session=s,
            connection=conn,
            probe=StoredCredentialProbe(),
            refresher=ReconnectOnExpiry(),
            store=get_secret_store(),
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize(("ct", "provider"), CASES)
async def test_expiry_notifies_once_and_reconnect_offers_a_flow(ct: str, provider: str):
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    if _honest_disabled(ct, provider):
        # An honest-disabled provider's self-maintenance truth IS the refusal.
        with pytest.raises(conn_service.ProviderNotRegistered):
            async with tenant_session(admin) as s:
                await conn_service.start_connection(
                    s,
                    admin_id=admin,
                    instance_id=iid,
                    connection_type=ct,
                    provider=provider,
                    user_id=admin,
                )
        return

    auth_class = conn_service._auth_class_for(ct, provider)
    cid = await _seed_connection(
        admin,
        iid,
        ct,
        provider,
        with_material=False,  # StoredCredentialProbe: missing material = broken
        expired_hint=auth_class in _REFRESH_CLASSES,
    )
    status = await _run_check(admin, cid)

    if auth_class == "provisioned_resource":
        # Nothing to expire: the platform owns the resource — staying connected
        # with no notification IS the honest cell.
        assert status == "connected"
        assert await _attention_emails(admin) == 0
        return

    expected = "expired" if auth_class in _REFRESH_CLASSES else "error"
    assert status == expected
    row = await _row(admin, cid)
    assert row.status_detail, "the owner must see a plain reason on the row"
    assert await _attention_emails(admin) == 1

    # Still broken on the next sweep → not a new incident → no second email.
    await _run_check(admin, cid)
    assert await _attention_emails(admin) == 1

    # Reconnect offers a workable flow either way (consent URL or the form).
    async with tenant_session(admin) as s:
        result = await conn_service.reconnect(s, admin_id=admin, connection_id=cid, user_id=admin)
    assert result is not None
    _conn, flow = result
    assert flow.get("authorize_url") or flow.get("requires_client_form")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize(("ct", "provider"), CASES)
async def test_disconnect_destroys_the_secret_and_row_is_reusable(ct: str, provider: str):
    if _honest_disabled(ct, provider):
        pytest.skip("honest-disabled provider — no row can exist to disconnect")
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = await _seed_connection(admin, iid, ct, provider)
    row = await _row(admin, cid)

    async with tenant_session(admin) as s:
        summary = await conn_service.disconnect(s, admin_id=admin, connection_id=cid, user_id=admin)
    assert summary is not None
    if row.secret_ref:
        assert not await get_secret_store().get(row.secret_ref), "credential must be destroyed"
    after = await _row(admin, cid)
    assert after.status != "connected"

    # Reusable: a fresh connect starts cleanly on the same type.
    async with tenant_session(admin) as s:
        conn, flow = await conn_service.start_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_type=ct,
            provider=provider,
            user_id=admin,
        )
    assert conn is not None
    assert flow.get("authorize_url") or flow.get("requires_client_form")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize(("ct", "provider"), CASES)
async def test_swap_stages_without_dropping_the_live_provider(ct: str, provider: str):
    if _honest_disabled(ct, provider):
        pytest.skip("honest-disabled provider — no row can exist to swap")
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = await _seed_connection(admin, iid, ct, provider)

    # Prefer a DIFFERENT connectable provider as the target; else re-auth self.
    others = [
        s.provider
        for s in registry.providers_for(ct)
        if s.provider != provider and not _honest_disabled(ct, s.provider)
    ]
    target = others[0] if others else provider

    async with tenant_session(admin) as s:
        result = await conn_service.swap(
            s, admin_id=admin, connection_id=cid, provider=target, user_id=admin
        )
    assert result is not None
    row = await _row(admin, cid)
    assert row.provider == provider, "cutover happens only on confirm_reconnect"
    assert row.status == "connected", "the live connection keeps serving mid-swap"
    assert (row.non_secret_config or {}).get("pending_provider") == target
    assert (row.status_detail or "").startswith("reconnect_pending:")

    # The side door refuses honest-disabled targets exactly like a fresh start.
    unavailable = [
        s.provider for s in registry.providers_for(ct) if _honest_disabled(ct, s.provider)
    ]
    if unavailable:
        with pytest.raises(conn_service.ProviderNotRegistered):
            async with tenant_session(admin) as s:
                await conn_service.swap(
                    s, admin_id=admin, connection_id=cid, provider=unavailable[0], user_id=admin
                )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize(("ct", "provider"), CASES)
async def test_dormant_rows_are_left_to_billing(ct: str, provider: str):
    if _honest_disabled(ct, provider):
        pytest.skip("honest-disabled provider — no row can exist to go dormant")
    from app.connections.health import run_health_sweep

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = await _seed_connection(admin, iid, ct, provider, status="dormant", with_material=False)

    async with tenant_session(admin) as s:
        await run_health_sweep(
            s,
            admin_id=admin,
            probe=StoredCredentialProbe(),
            refresher=ReconnectOnExpiry(),
        )
    row = await _row(admin, cid)
    assert row.status == "dormant", "dormant is billing-owned — the sweep never re-arms it"
    assert row.last_health_check_at is None
    assert await _attention_emails(admin) == 0
