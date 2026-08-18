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
from datetime import UTC, datetime, timedelta
from typing import Annotated, cast

import httpx
from fastapi import APIRouter, Depends, Query, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CLOSED_ACCOUNT_STATES, Subject, current_admin
from app.api.errors import conflict, not_found, validation_error
from app.audit import log as audit
from app.channels import routing
from app.connections import oauth_flow, registry, service
from app.connections.secrets import get_secret_store
from app.connections.verify import POST_EXCHANGE_VERIFIERS, PostExchangeVerifyFailed
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
from app.models.instance import Account, Instance
from app.schemas_api.connections import (
    BindDestinationRequest,
    ConnectionOut,
    ConnectionProvidersOut,
    DisconnectResult,
    EmailProvisioningOut,
    ProviderOptionOut,
    ProvisionEmailRequest,
    RecordSourceCsvOut,
    StartConnectionRequest,
    StartConnectionResult,
    SubmitCredentialsRequest,
    SwapConnectionRequest,
    SwitchConnectionRequest,
)
from app.schemas_api.knowledge import OAuthCallbackRequest

router = APIRouter(prefix="/api/v1/admin/connections", tags=["connections"])


def oauth_transport() -> httpx.AsyncClient:
    """Factory for the token-exchange transport, so tests can substitute a FAKE with no
    real network call (same seam the knowledge callback uses)."""
    return httpx.AsyncClient(timeout=15.0)


def _provider_display_name(provider: str) -> str:
    """The human label for a provider code, falling back to the raw code ONLY when
    the provider is not (or no longer) registered at all -- e.g. a connection made
    under a provider later dropped from the catalog. Every REGISTERED provider has
    a real display_name (Harmony wave 2, item 4 audited every entry in
    app.connections.registry.PROVIDERS for a raw-code leak; none remain)."""
    spec = registry.get_provider(provider)
    return spec.display_name if spec is not None else provider


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
            # Harmony wave 2 (item 3): registry.provider_available is the SAME check
            # /connections/providers uses for ProviderOptionOut.configured, so a
            # stale row for a provider the platform cannot connect today is marked
            # here too instead of rendering the owner-actionable "Action needed".
            "providerAvailable": registry.provider_available(c.provider),
            # Harmony wave 2 (item 4): give Overview the SAME human label Configure
            # already shows, so it never has to derive one from the raw provider
            # code (that derivation is what produced "Ses" instead of "Amazon SES").
            "displayName": _provider_display_name(c.provider),
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
        try:
            conn = await service.provision_email(
                s,
                admin_id=subject.admin_id,
                instance_id=inst.id,
                mode=req.mode,
                email_address=req.email_address,
                instance_name=inst.name,
                user_id=subject.user_id,
            )
        except service.EmailAddressTaken as exc:
            raise conflict(str(exc)) from exc
        return _email_provisioning_out(conn)


@router.post("", response_model=StartConnectionResult)
async def start_connection(
    req: StartConnectionRequest, subject: Subject = Depends(current_admin)
) -> StartConnectionResult:
    from app.channels.phone_lifecycle import NumberAlreadyClaimed  # noqa: PLC0415

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
        except NumberAlreadyClaimed as exc:
            raise conflict(str(exc)) from exc
        return StartConnectionResult.model_validate(result)


@router.post("/{connection_id}/credentials", response_model=ConnectionOut)
async def submit_credentials(
    connection_id: uuid.UUID,
    req: SubmitCredentialsRequest,
    subject: Subject = Depends(current_admin),
) -> ConnectionOut:
    """Finish a ``requiresClientForm`` connect by handing over the credential.

    The OAuth half of the lifecycle had a callback; the credential-form half had nothing,
    so a customer's own Twilio / webhook / live-connector credential had no way into the
    system and BYO telephony fell back to the platform's account. Same lifecycle either
    way: verify first, store the secret behind ``secret_ref``, then move the status.

    Write-only for secrets (§3.8.3): the response is the ordinary non-secret connection
    projection, so nothing submitted here can be read back out."""
    async with tenant_session(subject.admin_id) as s:
        try:
            conn = await service.submit_credentials(
                s,
                admin_id=subject.admin_id,
                connection_id=connection_id,
                fields=req.fields,
                user_id=subject.user_id,
            )
        except service.CredentialRejected as exc:
            raise validation_error(str(exc)) from exc
        if conn is None:
            raise not_found("Connection not found.")
        return _connection_out(conn)


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


@router.post("/email/reverify", response_model=ConnectionOut)
async def reverify_email_routing_endpoint(
    subject: Subject = Depends(current_admin),
) -> ConnectionOut:
    """Re-run the MX-delegation probe against the tenant's CURRENTLY provisioned
    own_domain email address (§3.1.6a, harmony fix 2026-08-04 item 5). Mirrors
    ``/connections/sms/reverify``: stateless + repeatable (no background poller, no
    expiry) — the tenant clicks this after adding the MX record shown by
    ``GET .../email`` at their domain registrar. Transitions to ``connected`` only on a
    confirmed live DELEGATED result; a domain that still doesn't resolve our MX stays in
    the honest ``pending_email_routing`` state — closing the previous dead end where
    that status had no way forward once set. vm_subdomain connections (no DNS step) are
    returned unchanged. Reads the current address live, so a since-changed address is
    what gets checked."""
    async with tenant_session(subject.admin_id) as s:
        instance_id = await _instance_id(s, subject.admin_id)
        conn = await service.reverify_email_routing(
            s, admin_id=subject.admin_id, instance_id=instance_id
        )
        if conn is None:
            raise not_found("No email address provisioned to re-verify.")
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
    connection_type: Annotated[str | None, Query()] = None,
    connection_type_camel: Annotated[str | None, Query(alias="connectionType")] = None,
    subject: Subject = Depends(current_admin),
) -> list[ConnectionProvidersOut]:
    """The provider CHOICES the owner may pick from, per connection type (Decision #6).

    The UI used to hardcode HubSpot for push_to_crm and Salesforce for lookup_record, so a
    customer on a different CRM was simply stuck. This is the registry projected onto the
    wire so the choice is rendered from server truth. Non-secret by construction: only a
    ``configured`` boolean is derived from the platform OAuth credential (§3.8.3).

    BOTH spellings of the filter are accepted. Every other field on this API is camelCase,
    so callers reasonably send ``connectionType``; the original snake_case-only parameter
    meant an unknown query param was silently dropped and the WHOLE catalog came back —
    which is how Google Calendar ended up rendered under CRM, look-up and the channels.
    An ignored filter must not be able to widen a response, so both names bind here.

    Read-only over static registry data, so it needs no tenant session — but it stays
    admin-authed because the set of integrations we offer is not public information."""
    del subject
    selected = connection_type or connection_type_camel
    types = [selected] if selected else sorted(registry.CHOICE_TYPES)
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
                            # unavailable_for wins over OAuth-client presence: a
                            # provider whose EFFECT layer is unbuilt for this
                            # surface must render "not available yet", never a
                            # connect button that dead-ends (honest-disabled).
                            "configured": False
                            if ct in spec.unavailable_for
                            else (
                                registry.oauth_client_config(spec.provider).configured
                                if spec.is_oauth
                                else True
                            ),
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


# Record-source CSV bounds: a live-lookup table, not the knowledge store — the
# knowledge quota (5 GB) does not apply here. Bounded so one upload cannot
# balloon a hot tenant table the tool scans at call time.
RECORD_SOURCE_MAX_BYTES = 5_000_000
RECORD_SOURCE_MAX_ROWS = 10_000


@router.post("/record-source/csv", response_model=RecordSourceCsvOut)
async def upload_record_source_csv(
    file: UploadFile, subject: Subject = Depends(current_admin)
) -> RecordSourceCsvOut:
    """Upload (or REPLACE) the CSV rows backing ``lookup_record`` (Vision §3.2).

    The rows are the ``csv`` provider's live system of record — queried by the
    tool at call time, never via the knowledge store (§3.2 boundary). Upload
    replaces the tenant's previous rows in one transaction and flips the
    ``record_source`` connection to ``connected`` with the row count on its
    status detail, so the Configure surface states exactly what the tool serves.
    """
    import csv as csv_module  # noqa: PLC0415
    import io  # noqa: PLC0415

    from sqlalchemy import delete as sa_delete  # noqa: PLC0415

    from app.models.record_source import RecordSourceRecord  # noqa: PLC0415

    raw = await file.read(RECORD_SOURCE_MAX_BYTES + 1)
    if len(raw) > RECORD_SOURCE_MAX_BYTES:
        raise validation_error("That CSV is larger than 5 MB — split it or trim unused columns.")
    reader = csv_module.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace")))
    columns = [c for c in (reader.fieldnames or []) if c]
    if not columns:
        raise validation_error("The CSV needs a header row naming its columns.")
    rows: list[dict[str, str]] = []
    for record in reader:
        if len(rows) >= RECORD_SOURCE_MAX_ROWS:
            raise validation_error(
                f"That CSV has more than {RECORD_SOURCE_MAX_ROWS} rows — trim it down."
            )
        fields = {str(k): str(v or "") for k, v in record.items() if k}
        if any(v.strip() for v in fields.values()):
            rows.append(fields)

    async with tenant_session(subject.admin_id) as s:
        instance_id = await _instance_id(s, subject.admin_id)
        await s.execute(
            sa_delete(RecordSourceRecord).where(RecordSourceRecord.admin_id == subject.admin_id)
        )
        for ordinal, fields in enumerate(rows):
            s.add(
                RecordSourceRecord(
                    admin_id=subject.admin_id,
                    instance_id=instance_id,
                    ordinal=ordinal,
                    fields=fields,
                )
            )
        conn = (
            await s.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == subject.admin_id,
                    InstanceConnection.instance_id == instance_id,
                    InstanceConnection.connection_type == "record_source",
                )
            )
        ).scalar_one_or_none()
        detail = f"{len(rows)} records on file"
        if conn is None:
            old_status = "unconfigured"
            conn = InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=subject.admin_id,
                instance_id=instance_id,
                connection_type="record_source",
                provider="csv",
                auth_class="provisioned_resource",
                status="connected",
                status_detail=detail,
            )
            s.add(conn)
        else:
            old_status = conn.status
            conn.provider = "csv"
            conn.auth_class = "provisioned_resource"
            conn.status = "connected"
            conn.status_detail = detail
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
                "reason": "record_source_csv_uploaded",
            },
        )
    return RecordSourceCsvOut(records=len(rows), columns=columns)


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
                "connection": _connection_out(cast("InstanceConnection", out["connection"])),
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


class OAuthStateRejected(Exception):
    """The returned ``state`` is missing, forged, expired, or for another attempt."""


class OAuthExchangeRejected(Exception):
    """The provider refused the code, or its token endpoint was unreachable."""


async def complete_oauth_connect(
    *,
    admin_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    connection_id: uuid.UUID,
    code: str,
    state: str | None,
) -> ConnectionOut:
    """Redeem an authorization code and mark the connection connected (Decisions #5/#7).

    The provider-agnostic half of the connect flow, shared by BOTH callback surfaces:
    the admin-authed ``POST .../oauth-callback`` (frontend-mediated) and the browser
    ``GET /api/v1/connections/{client}/callback`` the provider itself redirects to. The
    token endpoint, PKCE posture, and secret-ref convention all come from the registry,
    so there is no per-provider branch.

    ``state`` is MANDATORY and single-use (§5.1): it is read-and-cleared BEFORE the
    exchange, so a replayed callback finds no live attempt to ride. Without that check
    this would redeem a code obtained by any site that could drive the admin's browser
    here — CSRF straight into a tenant's connection.

    The exchange runs BETWEEN two committed transactions rather than inside one. The
    single transaction it replaces rolled back on any failure, which silently undid both
    the state consumption (so a burnt state came back to life) and the recorded reason —
    leaving the owner staring at an error and a connection that claimed nothing was wrong.
    """
    async with tenant_session(admin_id) as s:
        # Account state, checked here rather than only at the router (P1-5). The GET
        # callback is entered by the PROVIDER's redirect and carries no session, so
        # ``current_admin`` never runs on it: a consent screen opened before closure could
        # otherwise be completed after it, writing fresh credentials into Secrets Manager
        # for a tenant whose secrets the deletion cascade has already swept.
        acct = (await s.execute(select(Account).where(Account.id == admin_id))).scalar_one_or_none()
        if acct is None or acct.state in CLOSED_ACCOUNT_STATES:
            raise OAuthStateRejected("Account is closed; the connection cannot be completed.")
        conn = await _tenant_connection(s, admin_id, connection_id)
        # The EFFECTIVE provider: a swap stages its candidate in non_secret_config
        # (§3.8.7 rule B) and never touches `conn.provider` before cutover — so the
        # callback for a ses→outlook swap must exchange against OUTLOOK's endpoints.
        # Resolving from the row alone made every cross-provider swap unredeemable:
        # the live provider was provisioned-class, so this raised before the exchange.
        staged = (conn.non_secret_config or {}).get("pending_provider")
        pending_provider = staged if isinstance(staged, str) and staged else None
        provider = pending_provider or conn.provider
        spec = registry.get_provider(provider)
        if spec is None or not spec.is_oauth:
            raise OAuthStateRejected(
                f"Provider '{provider}' has no OAuth callback (not an OAuth-class provider)."
            )
        connection_type = conn.connection_type
        instance_id = await _instance_id(s, admin_id)
        expected_jti = service.consume_oauth_state(conn)
        state_rejected = None
        try:
            oauth_flow.verify_state(
                state,
                admin_id=admin_id,
                connection_id=conn.connection_id,
                expected_jti=expected_jti,
            )
        except oauth_flow.StateRejected as exc:
            state_rejected = str(exc)
        # Commit the consumption either way: a state that has been offered once is spent,
        # whether or not it verified.
    if state_rejected is not None:
        raise OAuthStateRejected(state_rejected)

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
                code=code,
                transport=cast("HttpTransport", transport),
                code_verifier=verifier,
            )
        except OAuthExchangeError as exc:
            await _fail_exchange(admin_id, connection_id, actor_user_id, pending_provider, str(exc))
            raise OAuthExchangeRejected(f"OAuth token exchange failed: {exc}") from exc
        except ProviderUnavailable as exc:
            await _fail_exchange(admin_id, connection_id, actor_user_id, pending_provider, str(exc))
            raise OAuthExchangeRejected(str(exc)) from exc

        # The token is durable before the row claims to be connected, so a crash in
        # between leaves a retryable "not connected yet" rather than a connection with
        # no credential. It also runs before VERIFY on purpose: a verify retry can
        # reuse the stored token, whereas an unstored token is gone with the code.
        await store_oauth_token(
            get_secret_store(),
            admin_id=admin_id,
            instance_id=instance_id,
            material=material,
            connection_type=connection_type,
        )

        # §3.1.6a "connect, VERIFY, bind, then live": providers with a registered
        # verifier must prove the credential works — and reveal WHOSE it is — before
        # any status flips. Providers without one keep today's behavior exactly.
        verify_updates: dict[str, object] | None = None
        provider_verifier = POST_EXCHANGE_VERIFIERS.get(provider)
        if provider_verifier is not None:
            try:
                verify_updates = await provider_verifier(material=material, transport=transport)
            except PostExchangeVerifyFailed as exc:
                await _fail_exchange(
                    admin_id, connection_id, actor_user_id, pending_provider, str(exc)
                )
                raise OAuthExchangeRejected(str(exc)) from exc

    conflict_reason: str | None = None
    async with tenant_session(admin_id) as s:
        conn = await _tenant_connection(s, admin_id, connection_id)
        if verify_updates is not None:
            # BIND: the verified address becomes this tenant's inbound-routing
            # destination — unless another tenant already holds it. Checked through
            # the same SECURITY DEFINER resolver delivery uses, because RLS hides
            # foreign claims from an ordinary query (routing.owner_of_destination).
            destination = str(verify_updates.get("destination", ""))
            if destination:
                holder = await routing.owner_of_destination(s, connection_type, destination)
                if holder is not None and holder != admin_id:
                    conflict_reason = f"{destination} is already connected to a different account"
            if conflict_reason is None:
                cfg = dict(conn.non_secret_config or {})
                cfg.update(verify_updates)
                conn.non_secret_config = cfg
        # Generic token-expiry stamping (§3.8.5): every OAuth exchange that
        # states a lifetime gets its `token_expires_at` hint recorded, so the
        # proactive-refresh worker's expiry branch fires for EVERY provider —
        # not only Outlook, whose verifier (POST_EXCHANGE_VERIFIERS) stamps its
        # own value and is never overwritten here. Without this hint the sweep
        # had nothing to key on: Google stored a refresh token nothing ever
        # redeemed and the connection died ~an hour after connect.
        if conflict_reason is None and material.get("expires_in"):
            raw_lifetime = str(material["expires_in"])
            if raw_lifetime.isdigit() and "token_expires_at" not in (verify_updates or {}):
                cfg = dict(conn.non_secret_config or {})
                cfg["token_expires_at"] = (
                    datetime.now(UTC) + timedelta(seconds=int(raw_lifetime))
                ).isoformat()
                conn.non_secret_config = cfg
        if conflict_reason is None:
            if pending_provider is not None:
                # A swap completes through confirm_reconnect — the ONLY place a staged
                # provider becomes authoritative — so the cutover semantics (flip
                # provider+auth_class, clear the pending marker, audit) hold on the
                # real callback path and not just in tests.
                ok, detail = await service.confirm_reconnect(
                    s,
                    admin_id=admin_id,
                    connection_id=connection_id,
                    user_id=actor_user_id,
                    probe_ok=True,
                )
                if not ok:  # pragma: no cover — row vanished mid-flow
                    raise OAuthExchangeRejected(detail)
            else:
                old_status = conn.status
                conn.status = "connected"
                conn.status_detail = None
                await s.flush()
                await audit.emit(
                    s,
                    admin_id=admin_id,
                    instance_id=instance_id,
                    actor_type=ActorType.admin_user,
                    actor_user_id=actor_user_id,
                    event_type="connection_status_changed",
                    payload={
                        "connection_id": str(conn.connection_id),
                        "old_status": old_status,
                        "new_status": "connected",
                        "reason": "oauth_callback_token_exchanged",
                    },
                )
            return _connection_out(conn)

    # Destination conflict: fail exactly like a failed verify — previous sender stays
    # live, the owner gets an actionable reason, nothing half-binds.
    await _fail_exchange(admin_id, connection_id, actor_user_id, pending_provider, conflict_reason)
    raise OAuthExchangeRejected(conflict_reason)


async def _fail_exchange(
    admin_id: uuid.UUID,
    connection_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    pending_provider: str | None,
    reason: str,
) -> None:
    """Record a failed exchange/verify so the previous sender STAYS live.

    For a swap that means resolving through ``confirm_reconnect(probe_ok=False)`` —
    it restores the prior status from the reconnect marker and discards the staged
    provider, which is the mechanical form of "never silently replaces a working
    sender" (§3.1.6a). A plain connect just records the reason on the row.
    """
    if pending_provider is not None:
        async with tenant_session(admin_id) as s:
            await service.confirm_reconnect(
                s,
                admin_id=admin_id,
                connection_id=connection_id,
                user_id=actor_user_id,
                probe_ok=False,
                probe_detail=reason,
            )
    else:
        await service.record_exchange_failure(admin_id, connection_id, reason)


@router.post("/{connection_id}/oauth-callback", response_model=ConnectionOut)
async def oauth_callback(
    connection_id: uuid.UUID,
    req: OAuthCallbackRequest,
    subject: Subject = Depends(current_admin),
) -> ConnectionOut:
    """Complete an OAuth connect for any registry provider (Decisions #5/#7).

    A rejected state surfaces as a clean 422 and a rejected code as a clean 409, never
    a crash. See ``complete_oauth_connect`` for the flow this wraps.
    """
    try:
        return await complete_oauth_connect(
            admin_id=subject.admin_id,
            actor_user_id=subject.user_id,
            connection_id=connection_id,
            code=req.code,
            state=req.state,
        )
    except OAuthStateRejected as exc:
        raise validation_error(str(exc)) from exc
    except OAuthExchangeRejected as exc:
        raise conflict(str(exc)) from exc


# Which channels may name an asset on which grant. Two entries rather than one because
# Instagram Login is a separate Meta app with a separate token (see connections.registry).
_BINDABLE_CHANNELS: dict[str, frozenset[str]] = {
    "channel_auth": frozenset({"whatsapp", "messenger"}),
    "instagram_auth": frozenset({"instagram"}),
}


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

    Per-channel by design (§3.1.4/§3.8.1): the Facebook grant authorizes WhatsApp and
    Messenger, and each names its own asset under ``destinations``. Enabling a second
    channel on a grant therefore adds a destination rather than replacing one — the
    eviction that used to happen when they were separate connections. Instagram rides its
    own grant (``instagram_auth``) and so binds on its own connection; the channel is
    checked against the connection's grant here, because a page id written under the wrong
    one resolves to no tenant at delivery time and the owner is told nothing.
    """
    async with tenant_session(subject.admin_id) as s:
        conn = await _tenant_connection(s, subject.admin_id, connection_id)
        allowed = _BINDABLE_CHANNELS.get(conn.connection_type)
        if allowed is None:
            raise validation_error(
                f"'{conn.connection_type}' destinations are not bound here (only "
                "channel_auth and instagram_auth; an SMS number is attached via "
                "POST /connections)."
            )
        if req.channel and req.channel not in allowed:
            raise validation_error(
                f"'{req.channel}' is not a channel on this connection — it binds on the "
                f"{'Instagram' if req.channel == 'instagram' else 'Meta'} connection."
            )
        instance_id = await _instance_id(s, subject.admin_id)
        owner = await routing.owner_of_destination(s, conn.connection_type, req.destination)
        if owner is not None and owner != subject.admin_id:
            raise conflict(
                "That Meta destination is already connected to another account. "
                "Disconnect it there first."
            )
        cfg = dict(conn.non_secret_config or {})
        if req.channel:
            existing_map = cfg.get("destinations")
            destinations = dict(existing_map) if isinstance(existing_map, dict) else {}
            previous = destinations.get(req.channel)
            destinations[req.channel] = req.destination
            cfg["destinations"] = destinations
        else:
            previous = cfg.get("destination")
            cfg["destination"] = req.destination
        # New dict, not an in-place edit: the JSONB column is not a MutableDict, so
        # mutating the existing one would not be flushed.
        conn.non_secret_config = cfg
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
                "channel": req.channel,
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
