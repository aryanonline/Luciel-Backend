"""Connections service: create / reconnect / revoke (Architecture §3.8.2/§3.8.4).

ONE mechanism for every external dependency — tools, channels, knowledge sources,
escalation delivery all reuse ``instance_connections`` (§3.8: no second pillar).

Connect-flow shape (frozen StartConnectionResult):
  * OAuth-class providers return ``authorizeUrl`` — the UI redirects the admin to
    the provider consent screen; the callback completion that exchanges the code
    for tokens and writes them to the SecretStore is an AWS/provider-DEFERRED SEAM
    (no real OAuth round-trip in this arc).
  * Non-OAuth providers (CSV upload, raw webhook URL, pasted API key) return
    ``requiresClientForm: true`` — the UI collects the fields client-side and the
    secret is handed to the SecretStore server-side later.

§3.8.3 secret hygiene: this module writes ONLY ``secret_ref`` (a pointer, never the
secret) onto the row; the secret itself lives in the SecretStore behind its seam.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.connections.secrets import SecretStore, get_secret_store, secret_ref_for
from app.models.audit import ActorType
from app.models.connection import InstanceConnection

# §3.8.2: auth_class is the behavior key (drives §3.8.5 health cadence + refresh),
# defaulted from connection_type. The provider brand never reaches this table's
# logic — a new provider in an existing class inherits behavior automatically.
_AUTH_CLASS_BY_TYPE: dict[str, str] = {
    "calendar": "oauth_token",
    "crm": "oauth_token",
    "channel_auth": "oauth_token",
    "knowledge_source": "oauth_token",
    "email_sender": "api_key",
    "sms_sender": "api_key",
    "record_source": "provisioned_resource",
    "outbound_webhook": "long_lived_token",
}
# OAuth-class connect flows return an authorizeUrl; everything else collects fields
# client-side (requiresClientForm). Keyed on auth_class so the catalog can grow.
_OAUTH_CLASSES = frozenset({"oauth_token"})


def _auth_class_for(connection_type: str) -> str:
    # Unknown types fall back to api_key (safe, 60-min cadence) rather than erroring,
    # so the catalog can add a type before this map is updated (§3.8.5 cadence table).
    return _AUTH_CLASS_BY_TYPE.get(connection_type, "api_key")


def _start_result(auth_class: str, connection: InstanceConnection) -> dict[str, object]:
    """Frozen StartConnectionResult: authorizeUrl (OAuth) xor requiresClientForm."""
    if auth_class in _OAUTH_CLASSES:
        # AWS/provider-DEFERRED SEAM: the real provider authorize URL (client_id,
        # scopes, PKCE, signed state) binds when OAuth lands. Here we hand back a
        # deterministic internal placeholder the UI can redirect to; the callback
        # that completes the exchange is likewise deferred.
        return {"authorize_url": f"/api/v1/admin/connections/{connection.connection_id}/authorize"}
    return {"requires_client_form": True}


async def _single_active(
    session: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID, connection_type: str
) -> InstanceConnection | None:
    """The current non-revoked connection of this type, if any (§3.8.2 single-active)."""
    return (
        await session.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.instance_id == instance_id,
                InstanceConnection.connection_type == connection_type,
                InstanceConnection.status != "revoked",
            )
        )
    ).scalar_one_or_none()


async def list_connections(session: AsyncSession, admin_id: uuid.UUID) -> list[InstanceConnection]:
    """All non-revoked connections for the tenant (§3.8.4: revoked rows are retained
    but not surfaced in the live list)."""
    return list(
        (
            await session.execute(
                select(InstanceConnection)
                .where(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.status != "revoked",
                )
                .order_by(InstanceConnection.created_at)
            )
        )
        .scalars()
        .all()
    )


async def start_connection(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    connection_type: str,
    provider: str,
    user_id: uuid.UUID | None,
    now: datetime | None = None,
) -> tuple[InstanceConnection, dict[str, object]]:
    """Begin a connect flow: create (or reuse) the row in ``unconfigured`` and return
    the row + StartConnectionResult (§3.8.4 lifecycle starts at unconfigured).

    If a non-revoked row of this type already exists we reuse it (single-active per
    type, §3.8.2) and reset it to unconfigured so the admin can reconfigure."""
    now = now or datetime.now(UTC)
    auth_class = _auth_class_for(connection_type)

    existing = await _single_active(session, admin_id, instance_id, connection_type)
    if existing is not None:
        existing.provider = provider
        existing.auth_class = auth_class
        existing.status = "unconfigured"
        existing.status_detail = None
        connection = existing
    else:
        connection = InstanceConnection(
            connection_id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            connection_type=connection_type,
            auth_class=auth_class,
            provider=provider,
            status="unconfigured",
            secret_ref=secret_ref_for(admin_id, instance_id, connection_type),
            created_by_user_id=user_id,
        )
        session.add(connection)
    await session.flush()

    # §3.7.1/§3.8: configuring a dependency is an admin-config event.
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        actor_type=ActorType.admin_user,
        actor_user_id=user_id,
        event_type="instance_configured",
        payload={
            "connection_id": str(connection.connection_id),
            "connection_type": connection_type,
            "provider": provider,
            "action": "start_connection",
        },
    )
    return connection, _start_result(auth_class, connection)


async def reconnect(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    connection_id: uuid.UUID,
    user_id: uuid.UUID | None,
) -> tuple[InstanceConnection, dict[str, object]] | None:
    """Re-initiate a connect flow: proven-before-cutover (Decision §45 / §3.8.7 rule B).

    The EXISTING connection is kept LIVE while the new credential flow is prepared.
    Status is NOT immediately dropped to ``unconfigured``; that would take the live
    dependency offline before the new credential is confirmed healthy.  Instead:

      1. ``status_detail`` records ``reconnect_pending:prior_status=<old>`` so the
         confirm step (and the health worker) can roll back if the probe fails.
      2. When the OAuth callback / credential submission arrives, the caller invokes
         ``confirm_reconnect()`` which health-checks the new credential and only then
         cuts over to ``connected`` (or restores the prior status on failure).

    Returns None if the connection does not exist for this tenant."""
    connection = (
        await session.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.connection_id == connection_id,
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        return None

    # Proven-before-cutover: record intent without taking the live connection offline.
    old_status = connection.status
    connection.status_detail = f"reconnect_pending:prior_status={old_status}"
    await session.flush()

    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=connection.instance_id,
        actor_type=ActorType.admin_user,
        actor_user_id=user_id,
        event_type="connection_status_changed",
        payload={
            "connection_id": str(connection.connection_id),
            "old_status": old_status,
            "new_status": old_status,  # preserved — cutover only on health confirm
            "reason": "reconnect_pending",
        },
    )
    return connection, _start_result(connection.auth_class, connection)


async def confirm_reconnect(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    connection_id: uuid.UUID,
    user_id: uuid.UUID | None,
    probe_ok: bool,
    probe_detail: str | None = None,
) -> tuple[bool, str]:
    """Complete a proven-before-cutover reconnect (Decision §45 / §3.8.7 rule B).

    Called by the OAuth callback / credential submission handler AFTER health-checking
    the new credential externally.  ``probe_ok`` is the result of that health check.

      * On success — promotes status to ``connected``, clears the pending detail.
      * On failure — restores the prior status from ``status_detail``, surfaces the
        error; the old connection remains live.

    Returns (True, 'connected') on success, (False, reason) on failure.
    Returns (False, 'not_found') if the connection does not exist for this tenant."""
    connection = (
        await session.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.connection_id == connection_id,
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        return False, "not_found"

    detail = connection.status_detail or ""
    prior_status = "unconfigured"
    if detail.startswith("reconnect_pending:prior_status="):
        prior_status = detail.split("=", 1)[1]

    if probe_ok:
        # New credential confirmed healthy — cut over now (proven-before-cutover).
        connection.status = "connected"
        connection.status_detail = None
        await session.flush()
        await audit.emit(
            session,
            admin_id=admin_id,
            instance_id=connection.instance_id,
            actor_type=ActorType.admin_user,
            actor_user_id=user_id,
            event_type="connection_status_changed",
            payload={
                "connection_id": str(connection.connection_id),
                "old_status": prior_status,
                "new_status": "connected",
                "reason": "reconnect_confirmed",
            },
        )
        return True, "connected"
    else:
        # Health check failed — keep the prior connection live (Decision §45 rule B).
        connection.status = prior_status
        reason = probe_detail or "reconnect_health_check_failed"
        connection.status_detail = reason
        await session.flush()
        await audit.emit(
            session,
            admin_id=admin_id,
            instance_id=connection.instance_id,
            actor_type=ActorType.admin_user,
            actor_user_id=user_id,
            event_type="connection_status_changed",
            payload={
                "connection_id": str(connection.connection_id),
                "old_status": prior_status,
                "new_status": prior_status,  # preserved — kept live
                "reason": "reconnect_health_check_failed",
            },
        )
        return False, reason


async def revoke(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    connection_id: uuid.UUID,
    user_id: uuid.UUID | None,
    store: SecretStore | None = None,
) -> bool:
    """Disconnect: set status=revoked + delete the secret; DO NOT hard-delete the row.

    §3.8.4: revoked connections are retained (audit history, possible later re-use)
    but are not broker-queryable. §3.6.3 cleanup: the secret is removed from the
    SecretStore (idempotent delete). Returns False if no such connection for tenant."""
    store = store or get_secret_store()
    connection = (
        await session.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.connection_id == connection_id,
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        return False

    old_status = connection.status
    connection.status = "revoked"
    connection.status_detail = "revoked by admin"
    await session.flush()

    # §3.6.3 secret cleanup: delete the secret material (idempotent — safe even if
    # nothing was ever stored, e.g. an unconfigured connection). The row's secret_ref
    # is kept as the audit pointer; the secret behind it is gone.
    deleted = False
    if connection.secret_ref is not None:
        deleted = await store.delete(connection.secret_ref)

    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=connection.instance_id,
        actor_type=ActorType.admin_user,
        actor_user_id=user_id,
        event_type="connection_status_changed",
        payload={
            "connection_id": str(connection.connection_id),
            "old_status": old_status,
            "new_status": "revoked",
            "reason": "disconnect",
        },
    )
    # §3.8.3 rule 4: cleanup audit carries NO secret material — only the ref + result.
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=connection.instance_id,
        actor_type=ActorType.admin_user,
        actor_user_id=user_id,
        event_type="connection_secret_cleanup",
        payload={
            "connection_id": str(connection.connection_id),
            "secret_ref": connection.secret_ref,
            "deleted": deleted,
        },
    )
    return True
