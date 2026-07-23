"""/api/v1/admin/connections/* — the admin Connections API (Architecture §3.8).

Implements the frozen admin client (http-admin.ts connections.*) field-for-field:
  * GET    /connections                      → Connection[] (non-secret only, §3.8.3)
  * POST   /connections {connectionType,...}  → StartConnectionResult
  * POST   /connections/{id}/reconnect        → StartConnectionResult
  * POST   /connections/{id}/swap {provider}   → StartConnectionResult (§3.8.7 rule B)
  * DELETE /connections/{id}                  → 204 (status→revoked + secret cleanup)
  * GET    /connections/email                 → EmailProvisioning | null (§3.1.6a)
  * POST   /connections/email {mode,...}       → EmailProvisioning (§3.1.6a)

All endpoints are admin-authed (verified account, §3.7.2) and run inside
``tenant_session`` so Layer-3 RLS scopes every row to the caller's tenant. Writes
are audited by the service layer. Secret material is NEVER accepted or returned
here — the wire model has no field for it (§3.8.3).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Subject, current_admin
from app.api.errors import not_found
from app.connections import service
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from app.models.instance import Instance
from app.schemas_api.connections import (
    ConnectionOut,
    EmailProvisioningOut,
    ProvisionEmailRequest,
    StartConnectionRequest,
    StartConnectionResult,
    SwapConnectionRequest,
)

router = APIRouter(prefix="/api/v1/admin/connections", tags=["connections"])


def _connection_out(c: InstanceConnection) -> ConnectionOut:
    # Build EXPLICITLY from non-secret fields only (§3.8.3). secret_ref / auth_class
    # are intentionally NOT mapped — the wire model has no slot for them anyway.
    return ConnectionOut.model_validate(
        {
            "connectionId": c.connection_id,
            "connectionType": c.connection_type,
            "provider": c.provider,
            "status": c.status,
            "nonSecretConfig": c.non_secret_config,
            "statusDetail": c.status_detail,
            "lastHealthCheckAt": c.last_health_check_at,
            "createdAt": c.created_at,
        }
    )


async def _instance_id(s: AsyncSession, admin_id: uuid.UUID) -> uuid.UUID:
    inst = (
        await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
    ).scalar_one_or_none()
    if inst is None:
        raise not_found("No Luciel.")
    return inst


async def _instance_row(s: AsyncSession, admin_id: uuid.UUID) -> Instance:
    inst = (
        await s.execute(select(Instance).where(Instance.admin_id == admin_id))
    ).scalar_one_or_none()
    if inst is None:
        raise not_found("No Luciel.")
    return inst


def _email_provisioning_out(c: InstanceConnection) -> EmailProvisioningOut:
    # Non-secret projection (§3.8.3): mode + address + status from non_secret_config;
    # DNS records are derived (own_domain only). No secret_ref, no auth_class.
    cfg = c.non_secret_config or {}
    address = str(cfg.get("address", ""))
    mode = str(cfg.get("mode", "vm_subdomain"))
    dns = service.dns_records_for(address) if mode == "own_domain" else None
    return EmailProvisioningOut.model_validate(
        {"mode": mode, "emailAddress": address, "status": c.status, "dnsRecords": dns}
    )


@router.get("", response_model=list[ConnectionOut])
async def list_connections(
    subject: Subject = Depends(current_admin),
) -> list[ConnectionOut]:
    async with tenant_session(subject.admin_id) as s:
        rows = await service.list_connections(s, subject.admin_id)
        return [_connection_out(c) for c in rows]


@router.get("/email", response_model=EmailProvisioningOut | None)
async def get_email_provisioning(
    subject: Subject = Depends(current_admin),
) -> EmailProvisioningOut | None:
    async with tenant_session(subject.admin_id) as s:
        conn = await service.get_email_provisioning(
            s, subject.admin_id, await _instance_id(s, subject.admin_id)
        )
        return _email_provisioning_out(conn) if conn is not None else None


@router.post("/email", response_model=EmailProvisioningOut)
async def provision_email(
    req: ProvisionEmailRequest, subject: Subject = Depends(current_admin)
) -> EmailProvisioningOut:
    async with tenant_session(subject.admin_id) as s:
        inst = await _instance_row(s, subject.admin_id)
        conn = await service.provision_email(
            s,
            admin_id=subject.admin_id,
            instance_id=inst.id,
            mode=req.mode,
            email_address=req.email_address,
            instance_name=inst.name,
            user_id=subject.user_id,
        )
        return _email_provisioning_out(conn)


@router.post("", response_model=StartConnectionResult)
async def start_connection(
    req: StartConnectionRequest, subject: Subject = Depends(current_admin)
) -> StartConnectionResult:
    async with tenant_session(subject.admin_id) as s:
        instance_id = await _instance_id(s, subject.admin_id)
        _, result = await service.start_connection(
            s,
            admin_id=subject.admin_id,
            instance_id=instance_id,
            connection_type=req.connection_type,
            provider=req.provider,
            user_id=subject.user_id,
            phone_number=req.phone_number,
        )
        return StartConnectionResult.model_validate(result)


@router.post("/{connection_id}/reconnect", response_model=StartConnectionResult)
async def reconnect_connection(
    connection_id: uuid.UUID, subject: Subject = Depends(current_admin)
) -> StartConnectionResult:
    async with tenant_session(subject.admin_id) as s:
        out = await service.reconnect(
            s,
            admin_id=subject.admin_id,
            connection_id=connection_id,
            user_id=subject.user_id,
        )
        if out is None:
            raise not_found("Connection not found.")
        _, result = out
        return StartConnectionResult.model_validate(result)


@router.post("/sms/reverify", response_model=ConnectionOut)
async def reverify_sms_number(subject: Subject = Depends(current_admin)) -> ConnectionOut:
    """Re-run the Twilio operability probe against the tenant's CURRENTLY attached SMS
    number (§3.1.4). Stateless + repeatable (no background poller, no expiry): the
    tenant clicks this after hosting/registering their number. Transitions to operable
    only on a confirmed live result; a still-non-operable number stays in the honest
    ``not_operable_hosting_required`` state. Reads the current number live, so a
    swapped-in number is what gets checked."""
    from app.channels.phone_lifecycle import reverify_sms_number as _reverify

    async with tenant_session(subject.admin_id) as s:
        instance_id = await _instance_id(s, subject.admin_id)
        conn = await _reverify(s, admin_id=subject.admin_id, instance_id=instance_id)
        if conn is None:
            raise not_found("No SMS number attached to re-verify.")
        return _connection_out(conn)


@router.post("/{connection_id}/swap", response_model=StartConnectionResult)
async def swap_connection(
    connection_id: uuid.UUID,
    req: SwapConnectionRequest,
    subject: Subject = Depends(current_admin),
) -> StartConnectionResult:
    """Change the connected account (§3.8.7 rule B): re-initiate the connect flow
    against a (possibly different) provider while the CURRENT connection stays live
    until the new credential verifies (proven-before-cutover, Decision §45)."""
    async with tenant_session(subject.admin_id) as s:
        out = await service.swap(
            s,
            admin_id=subject.admin_id,
            connection_id=connection_id,
            provider=req.provider,
            user_id=subject.user_id,
        )
        if out is None:
            raise not_found("Connection not found.")
        _, result = out
        return StartConnectionResult.model_validate(result)


@router.delete("/{connection_id}", status_code=204)
async def disconnect_connection(
    connection_id: uuid.UUID, response: Response, subject: Subject = Depends(current_admin)
) -> Response:
    async with tenant_session(subject.admin_id) as s:
        ok = await service.revoke(
            s,
            admin_id=subject.admin_id,
            connection_id=connection_id,
            user_id=subject.user_id,
        )
    if not ok:
        raise not_found("Connection not found.")
    response.status_code = 204
    return response
