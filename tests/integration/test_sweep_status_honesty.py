"""Round 5B items 10/11 — the health sweep's status taxonomy + probe honesty.

Live-caught 2026-08-24 on the dev walkthrough tenant: one 6-hour sweep flipped a
never-consented calendar row and an auth-header-less webhook CRM to a false
"error — stored credential is missing" (false attention email included), and the
same sweep's promotion bug was the only mechanism that ever made an SMS row read
``connected``. These pin the fix: the sweep owns ONLY the health statuses, the
probe only reports losses it can prove, and the operable→connected transition is
the owner's audited A2P attestation — never an accident.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels.phone_lifecycle import (
    AttestationNotReady,
    attest_carrier_registration,
)
from app.connections import service as conn_service
from app.connections.health import ReconnectOnExpiry, StoredCredentialProbe, run_health_sweep
from app.connections.secrets import get_secret_store, secret_ref_for
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select

# Round 7 WP-4a item 3 (owner ruling 2026-09-14): pending_carrier_registration and
# not_operable_hosting_required moved to the sweep's PROBE-ONLY allowlist — the
# credential behind them is re-checked (a text can leave on a pending row), but a
# passing probe never promotes them. Their proofs live in
# tests/integration/test_connection_lifecycle_truth.py; these three stay untouched.
SETUP_OWNED = (
    "unconfigured",
    "not_connected",
    "pending_email_routing",
)


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
                name="T",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:10]}",
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
    *,
    connection_type: str,
    provider: str,
    auth_class: str,
    status: str,
    detail: str | None = None,
    with_material: bool = False,
    config: dict | None = None,
) -> uuid.UUID:
    cid = uuid.uuid4()
    ref = secret_ref_for(admin, iid, connection_type)
    if with_material:
        await get_secret_store().put(ref, {"access_token": "tok"})
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=cid,
                admin_id=admin,
                instance_id=iid,
                connection_type=connection_type,
                provider=provider,
                auth_class=auth_class,
                status=status,
                status_detail=detail,
                secret_ref=ref,
                non_secret_config=config or {},
            )
        )
        await s.flush()
    return cid


async def _sweep(admin: uuid.UUID) -> dict[str, str]:
    async with tenant_session(admin) as s:
        return await run_health_sweep(
            s, admin_id=admin, probe=StoredCredentialProbe(), refresher=ReconnectOnExpiry()
        )


async def _row(admin: uuid.UUID, cid: uuid.UUID) -> InstanceConnection:
    async with tenant_session(admin) as s:
        return (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == cid)
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


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("status", SETUP_OWNED)
async def test_sweep_never_touches_setup_owned_statuses(status: str):
    """The live incident shape: a stamped secret_ref with no material used to flip
    these rows to error; a stored secret used to promote them to connected."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    detail = "setup guidance the owner still needs"
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="sms_sender",
        provider="twilio",
        auth_class="api_key",
        status=status,
        detail=detail,
        with_material=True,
    )
    result = await _sweep(admin)
    assert str(cid) not in result, "a setup-owned row must not even be selected"
    row = await _row(admin, cid)
    assert row.status == status
    assert row.status_detail == detail
    assert row.last_health_check_at is None
    assert await _attention_emails(admin) == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_staged_swap_marker_survives_a_sweep():
    """A FRESH marker (round 7 WP-4a item 5: one stamped within the abandonment
    window) shields the row; the abandoned-marker proofs live in
    test_connection_lifecycle_truth.py."""
    from datetime import UTC, datetime

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="crm",
        provider="hubspot",
        auth_class="oauth_token",
        status="connected",
        detail="reconnect_pending:prior_status=connected",
        with_material=True,
        config={"reconnect_pending_since": datetime.now(UTC).isoformat()},
    )
    await _sweep(admin)
    row = await _row(admin, cid)
    # The rollback target confirm_reconnect needs must not be erased (§3.8.7 B).
    assert row.status_detail == "reconnect_pending:prior_status=connected"
    assert row.last_health_check_at is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_secretless_webhook_crm_row_stays_connected():
    """A custom_webhook whose only field was the non-secret URL stores no secret
    material — the live-caught false 'stored credential is missing' shape."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="crm",
        provider="custom_webhook",
        auth_class="long_lived_token",
        status="connected",
        with_material=False,  # stamped ref, empty store
    )
    await _sweep(admin)
    row = await _row(admin, cid)
    assert row.status == "connected"
    assert await _attention_emails(admin) == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_legacy_ses_row_never_false_errors_or_emails():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="email_sender",
        provider="ses",
        auth_class="api_key",
        status="connected",
        with_material=False,
    )
    await _sweep(admin)
    row = await _row(admin, cid)
    assert row.status == "connected"
    assert await _attention_emails(admin) == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_vanished_oauth_secret_is_still_detected():
    """Guard against over-widening: a provider whose shape DOES hold a secret
    still errors + emails when the material is gone."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="calendar",
        provider="google_calendar",
        auth_class="oauth_token",
        status="connected",
        with_material=False,
    )
    await _sweep(admin)
    row = await _row(admin, cid)
    assert row.status == "error"
    assert await _attention_emails(admin) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_attestation_is_the_only_path_from_pending_to_connected():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    cid = await _seed_connection(
        admin,
        iid,
        connection_type="sms_sender",
        provider="twilio",
        auth_class="api_key",
        status="pending_carrier_registration",
        with_material=True,
        config={"destination": "+12365550100"},
    )
    # The sweep no longer promotes it…
    await _sweep(admin)
    assert (await _row(admin, cid)).status == "pending_carrier_registration"
    # …the owner's audited attestation does.
    async with tenant_session(admin) as s:
        conn = await attest_carrier_registration(s, admin_id=admin, instance_id=iid, user_id=admin)
        assert conn is not None
    row = await _row(admin, cid)
    assert row.status == "connected"
    assert "attestation" in (row.status_detail or "")
    cfg = row.non_secret_config or {}
    assert cfg.get("carrier_registration_attested_at")
    async with tenant_session(admin) as s:
        events = (
            (
                await s.execute(
                    select(AdminAuditLog.event_type).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "carrier_registration_attested",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    # Idempotent second call, still exactly one attestation event.
    async with tenant_session(admin) as s:
        await attest_carrier_registration(s, admin_id=admin, instance_id=iid, user_id=admin)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_attestation_refused_without_a_confirmed_number():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    await _seed_connection(
        admin,
        iid,
        connection_type="sms_sender",
        provider="twilio",
        auth_class="api_key",
        status="unconfigured",
        with_material=True,
        config={},
    )
    async with tenant_session(admin) as s:
        with pytest.raises(AttestationNotReady):
            await attest_carrier_registration(s, admin_id=admin, instance_id=iid, user_id=admin)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_dormant_row_cannot_be_rearmed_via_connect():
    """Round 5B item 16: dormancy is billing's suspension — Connect must refuse,
    not quietly reset the row and re-arm the tools."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    await _seed_connection(
        admin,
        iid,
        connection_type="calendar",
        provider="google_calendar",
        auth_class="oauth_token",
        status="dormant",
        with_material=True,
    )
    async with tenant_session(admin) as s:
        with pytest.raises(conn_service.DormantConnection):
            await conn_service.start_connection(
                s,
                admin_id=admin,
                instance_id=iid,
                connection_type="calendar",
                provider="google_calendar",
                user_id=admin,
            )
