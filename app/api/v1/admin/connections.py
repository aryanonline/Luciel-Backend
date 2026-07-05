"""/api/v1/admin/connections/* — the admin Connections API (Architecture §3.8).

Implements the frozen admin client (http-admin.ts connections.*) field-for-field:
  * GET    /connections                      → Connection[] (non-secret only, §3.8.3)
  * POST   /connections {connectionType,...}  → StartConnectionResult
  * POST   /connections/{id}/reconnect        → StartConnectionResult
  * DELETE /connections/{id}                  → 204 (status→revoked + secret cleanup)

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
    StartConnectionRequest,
    StartConnectionResult,
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


@router.get("", response_model=list[ConnectionOut])
async def list_connections(
    subject: Subject = Depends(current_admin),
) -> list[ConnectionOut]:
    async with tenant_session(subject.admin_id) as s:
        rows = await service.list_connections(s, subject.admin_id)
        return [_connection_out(c) for c in rows]


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
