"""/api/v1/admin/connections/* — the admin Connections API (Architecture §3.8).

Implements the frozen admin client (http-admin.ts connections.*) field-for-field:
  * GET    /connections                      → Connection[] (non-secret only, §3.8.3)
  * POST   /connections {connectionType,...}  → StartConnectionResult
  * POST   /connections/{id}/reconnect        → StartConnectionResult
  * POST   /connections/{id}/swap {provider}   → StartConnectionResult (§3.8.7 rule B)
  * GET    /connections/providers             → ConnectionProviders[] (Decision #6)
  * POST   /connections/{id}/disconnect       → DisconnectResult (Decision #5b)
  * POST   /connections/{id}/switch {provider} → StartConnectionResult (Decision #5c)
  * POST   /connections/{id}/oauth-callback   → Connection (Decision #7, any provider)
  * PUT    /connections/{id}/destination      → Connection (Decision #7, Meta asset)
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
from typing import cast

import httpx
from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Subject, current_admin
from app.api.errors import conflict, not_found, validation_error
from app.audit import log as audit
from app.channels import routing
from app.connections import oauth_flow, registry, service
from app.connections.secrets import get_secret_store
from app.db.session import tenant_session
from app.knowledge.providers import (
    HttpTransport,
    OAuthExchangeError,
    ProviderUnavailable,
    exchange_code_for_token,
    store_oauth_token,
)
from app.models.audit import ActorType
from app.models.connection import InstanceConnection
from app.models.instance import Instance
from app.schemas_api.connections import (
    BindDestinationRequest,
    ConnectionOut,
    ConnectionProvidersOut,
    DisconnectResult,
    EmailProvisioningOut,
    ProviderOptionOut,
    ProvisionEmailRequest,
    StartConnectionRequest,
    StartConnectionResult,
    SwapConnectionRequest,
    SwitchConnectionRequest,
)
from app.schemas_api.knowledge import OAuthCallbackRequest

router = APIRouter(prefix="/api/v1/admin/connections", tags=["connections"])


def oauth_transport() -> httpx.AsyncClient:
    """Factory for the token-exchange transport, so tests can substitute a FAKE with no
    real network call (same seam the knowledge callback uses)."""
    return httpx.AsyncClient(timeout=15.0)


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


async def _tenant_connection(
    s: AsyncSession, admin_id: uuid.UUID, connection_id: uuid.UUID
) -> InstanceConnection:
    """This tenant's connection row, or 404. RLS already scopes the session; the explicit
    ``admin_id`` predicate keeps the query honest on its own terms."""
    conn = (
        await s.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.connection_id == connection_id,
            )
        )
    ).scalar_one_or_none()
    if conn is None:
        raise not_found("Connection not found.")
    return conn


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
        try:
            _, result = await service.start_connection(
                s,
                admin_id=subject.admin_id,
                instance_id=instance_id,
                connection_type=req.connection_type,
                provider=req.provider,
                user_id=subject.user_id,
                phone_number=req.phone_number,
            )
        except service.ProviderNotRegistered as exc:
            raise validation_error(str(exc)) from exc
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


@router.get("/providers", response_model=list[ConnectionProvidersOut])
async def list_providers(
    connection_type: str | None = None,
    subject: Subject = Depends(current_admin),
) -> list[ConnectionProvidersOut]:
    """The provider CHOICES the owner may pick from, per connection type (Decision #6).

    The UI used to hardcode HubSpot for push_to_crm and Salesforce for lookup_record, so a
    customer on a different CRM was simply stuck. This is the registry projected onto the
    wire so the choice is rendered from server truth. Non-secret by construction: only a
    ``configured`` boolean is derived from the platform OAuth credential (§3.8.3).

    Read-only over static registry data, so it needs no tenant session — but it stays
    admin-authed because the set of integrations we offer is not public information."""
    del subject
    types = [connection_type] if connection_type else sorted(registry.CHOICE_TYPES)
    return [
        ConnectionProvidersOut.model_validate(
            {
                "connectionType": ct,
                "providers": [
                    ProviderOptionOut.model_validate(
                        {
                            "provider": spec.provider,
                            "displayName": spec.display_name,
                            "authKind": spec.auth_kind,
                            "helpText": spec.help_text,
                            "configured": registry.oauth_client_config(spec.provider).configured
                            if spec.is_oauth
                            else True,
                            "credentialFields": [
                                {
                                    "name": f.name,
                                    "label": f.label,
                                    "secret": f.secret,
                                    "required": f.required,
                                }
                                for f in spec.credential_fields
                            ],
                            "scopeKind": spec.scope_kind,
                        }
                    )
                    for spec in registry.providers_for(ct)
                ],
            }
        )
        for ct in types
    ]


@router.post("/{connection_id}/disconnect", response_model=DisconnectResult)
async def disconnect_connection_post(
    connection_id: uuid.UUID, subject: Subject = Depends(current_admin)
) -> DisconnectResult:
    """Hand a connection back (Decision #5b): destroy the stored credential, return the
    connection to a clean ``not_connected`` state it can be reconnected from, and disable
    the tools/channels that depended on it — reporting exactly which ones, so the owner is
    told that (e.g.) disconnecting the calendar took scheduling with it.

    Distinct from ``DELETE /{id}``, which drives the row to terminal ``revoked`` for
    account/Luciel teardown; that state is not reconnectable and is hidden from the list."""
    async with tenant_session(subject.admin_id) as s:
        out = await service.disconnect(
            s,
            admin_id=subject.admin_id,
            connection_id=connection_id,
            user_id=subject.user_id,
        )
        if out is None:
            raise not_found("Connection not found.")
        return DisconnectResult.model_validate(
            {
                "connection": _connection_out(out["connection"]),
                "secretDeleted": out["secret_deleted"],
                "disabledTools": out["disabled_tools"],
                "disabledChannels": out["disabled_channels"],
            }
        )


@router.post("/{connection_id}/switch", response_model=StartConnectionResult)
async def switch_connection(
    connection_id: uuid.UUID,
    req: SwitchConnectionRequest,
    subject: Subject = Depends(current_admin),
) -> StartConnectionResult:
    """Use a different account and/or provider (Decision #5c): disconnect, then begin a
    fresh connect flow against the chosen provider.

    Use this when the owner has decided the CURRENT account is wrong — the old credential
    is destroyed first, so nothing keeps serving from the account being left behind. Use
    ``/swap`` instead to re-credential an account that currently works, which keeps it
    live until the new credential verifies (§3.8.7 rule B)."""
    async with tenant_session(subject.admin_id) as s:
        try:
            out = await service.switch(
                s,
                admin_id=subject.admin_id,
                connection_id=connection_id,
                provider=req.provider,
                user_id=subject.user_id,
            )
        except service.ProviderNotRegistered as exc:
            raise validation_error(str(exc)) from exc
        if out is None:
            raise not_found("Connection not found.")
        _, result = out
        return StartConnectionResult.model_validate(result)


@router.post("/{connection_id}/oauth-callback", response_model=ConnectionOut)
async def oauth_callback(
    connection_id: uuid.UUID,
    req: OAuthCallbackRequest,
    subject: Subject = Depends(current_admin),
) -> ConnectionOut:
    """Complete an OAuth connect for any registry provider (Decisions #5/#7).

    Knowledge sync had its own callback; the Meta channels (and every future OAuth
    provider on a non-knowledge connection type) had none, so a `channel_auth` connect
    could reach a consent screen but never leave ``unconfigured``. This is the generic
    half: the token endpoint, PKCE posture, and secret-ref convention all come from the
    registry, so the route needs no per-provider branch.

    ``state`` is MANDATORY and single-use (§5.1): it is read-and-cleared BEFORE the
    exchange, so a replayed callback finds no live attempt to ride. Without that check
    the endpoint would redeem a code obtained by any site that could drive the admin's
    browser here — CSRF straight into a tenant's connection.

    The exchange runs BETWEEN two committed transactions rather than inside one. The
    single transaction it replaces rolled back on any failure, which silently undid both
    the state consumption (so a burnt state came back to life) and the recorded reason —
    leaving the owner staring at a 409 and a connection that claimed nothing was wrong.

    A rejected code surfaces as a clean 409, never a crash.
    """
    async with tenant_session(subject.admin_id) as s:
        conn = await _tenant_connection(s, subject.admin_id, connection_id)
        spec = registry.get_provider(conn.provider)
        if spec is None or not spec.is_oauth:
            raise validation_error(
                f"Provider '{conn.provider}' has no OAuth callback (not an OAuth-class provider)."
            )
        provider = conn.provider
        connection_type = conn.connection_type
        instance_id = await _instance_id(s, subject.admin_id)
        expected_jti = service.consume_oauth_state(conn)
        state_rejected = None
        try:
            oauth_flow.verify_state(
                req.state,
                admin_id=subject.admin_id,
                connection_id=conn.connection_id,
                expected_jti=expected_jti,
            )
        except oauth_flow.StateRejected as exc:
            state_rejected = str(exc)
        # Commit the consumption either way: a state that has been offered once is spent,
        # whether or not it verified.
    if state_rejected is not None:
        raise validation_error(state_rejected)

    authorize = spec.authorize
    verifier = (
        oauth_flow.verifier_for_jti(expected_jti)
        if authorize is not None and authorize.supports_pkce and expected_jti
        else None
    )
    async with oauth_transport() as transport:
        try:
            material = await exchange_code_for_token(
                provider,
                code=req.code,
                transport=cast("HttpTransport", transport),
                code_verifier=verifier,
            )
        except OAuthExchangeError as exc:
            await service.record_exchange_failure(subject.admin_id, connection_id, str(exc))
            raise conflict(f"OAuth token exchange failed: {exc}") from exc
        except ProviderUnavailable as exc:
            await service.record_exchange_failure(subject.admin_id, connection_id, str(exc))
            raise conflict(str(exc)) from exc

    # The token is durable before the row claims to be connected, so a crash in between
    # leaves a retryable "not connected yet" rather than a connection with no credential.
    await store_oauth_token(
        get_secret_store(),
        admin_id=subject.admin_id,
        instance_id=instance_id,
        material=material,
        connection_type=connection_type,
    )
    async with tenant_session(subject.admin_id) as s:
        conn = await _tenant_connection(s, subject.admin_id, connection_id)
        old_status = conn.status
        conn.status = "connected"
        conn.status_detail = None
        await s.flush()
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            instance_id=instance_id,
            actor_type=ActorType.admin_user,
            actor_user_id=subject.user_id,
            event_type="connection_status_changed",
            payload={
                "connection_id": str(conn.connection_id),
                "old_status": old_status,
                "new_status": "connected",
                "reason": "oauth_callback_token_exchanged",
            },
        )
        return _connection_out(conn)


@router.put("/{connection_id}/destination", response_model=ConnectionOut)
async def bind_destination(
    connection_id: uuid.UUID,
    req: BindDestinationRequest,
    subject: Subject = Depends(current_admin),
) -> ConnectionOut:
    """Name the Meta asset this Luciel answers on (Decision #7, completes the connect).

    The OAuth callback stores the token and the row says `connected`, but inbound routing
    resolves a tenant SOLELY by ``non_secret_config.destination`` (channels/routing.py) —
    so without this the owner has an authorized channel that silently receives nothing.
    Meta cannot supply it for us: an authorized user may own several WhatsApp numbers and
    several Pages, and only the owner knows which one this Luciel serves.

    The destination must be UNIQUE across tenants, and that is a security boundary rather
    than a tidiness rule: ``channel_resolve_destination`` returns the first matching row,
    so a tenant permitted to claim an id another tenant already holds could capture their
    inbound conversations. The check runs through that same resolver — the only thing that
    can see across tenants — so what we validate is exactly what routing will do.

    ``sms_sender`` is deliberately NOT bindable here: its number goes through
    ``attach_sms_number``, which also runs the Twilio operability + A2P status gate
    (§3.1.6). Allowing a raw write here would be a way around that gate.
    """
    async with tenant_session(subject.admin_id) as s:
        conn = await _tenant_connection(s, subject.admin_id, connection_id)
        if conn.connection_type != "channel_auth":
            raise validation_error(
                f"'{conn.connection_type}' destinations are not bound here "
                "(only channel_auth; an SMS number is attached via POST /connections)."
            )
        instance_id = await _instance_id(s, subject.admin_id)
        owner = await routing.owner_of_destination(s, "channel_auth", req.destination)
        if owner is not None and owner != subject.admin_id:
            raise conflict(
                "That Meta destination is already connected to another account. "
                "Disconnect it there first."
            )
        previous = (conn.non_secret_config or {}).get("destination")
        # New dict, not an in-place edit: the JSONB column is not a MutableDict, so
        # mutating the existing one would not be flushed.
        conn.non_secret_config = {**(conn.non_secret_config or {}), "destination": req.destination}
        await s.flush()
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            instance_id=instance_id,
            actor_type=ActorType.admin_user,
            actor_user_id=subject.user_id,
            event_type="instance_configured",
            payload={
                "connection_id": str(conn.connection_id),
                "connection_type": conn.connection_type,
                "provider": conn.provider,
                "action": "bind_destination",
                "old_destination": previous,
                "new_destination": req.destination,
            },
        )
        return _connection_out(conn)


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
