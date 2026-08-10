"""Connections service: create / reconnect / revoke (Architecture §3.8.2/§3.8.4).

ONE mechanism for every external dependency — tools, channels, knowledge sources,
escalation delivery all reuse ``instance_connections`` (§3.8: no second pillar).

Connect-flow shape (frozen StartConnectionResult):
  * OAuth-class providers return ``authorizeUrl`` — the provider's real consent URL
    when its OAuth client is registered in config (§5.1, see ``oauth_flow``), or an
    internal placeholder plus an "action needed" status_detail when it is not. The
    callback that exchanges the code for tokens lives in the knowledge API.
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
from app.channels.email_routing import MxDelegationProbe, MxDelegationResult
from app.connections import registry
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
    "instagram_auth": "oauth_token",
    "knowledge_source": "oauth_token",
    "email_sender": "api_key",
    "sms_sender": "api_key",
    "record_source": "provisioned_resource",
    "outbound_webhook": "long_lived_token",
}


class ProviderNotRegistered(ValueError):
    """The requested provider is not registered for this connection type (§3.8.2)."""


# OAuth-class connect flows return an authorizeUrl; everything else collects fields
# client-side (requiresClientForm). Keyed on auth_class so the catalog can grow.
_OAUTH_CLASSES = frozenset({"oauth_token"})


def _auth_class_for(connection_type: str, provider: str | None = None) -> str:
    """The behavior key for this connection (§3.8.5 health cadence + refresh).

    The registered PROVIDER wins when there is one, because the auth class is a property
    of the integration and not of the slot it fills: a CRM connection is oauth_token via
    HubSpot but long_lived_token via a custom webhook (Decision #6), and giving both the
    type's default would put the webhook on a token-refresh cadence it has no token for.
    Unknown types fall back to api_key (safe, 60-min cadence) rather than erroring, so the
    catalog can add a type before this map is updated."""
    spec = registry.get_provider(provider) if provider else None
    if spec is not None and spec.connection_type == connection_type:
        return spec.auth_class
    return _AUTH_CLASS_BY_TYPE.get(connection_type, "api_key")


def _start_result(
    auth_class: str, connection: InstanceConnection, *, admin_id: uuid.UUID
) -> dict[str, object]:
    """Frozen StartConnectionResult: authorizeUrl (OAuth) xor requiresClientForm.

    For OAuth-class rows this also BEGINS the attempt — the returned authorizeUrl is the
    provider's real consent URL (client_id, scopes, PKCE challenge, signed state), and
    the attempt's single-use state jti is parked on the row for the callback to check
    (§5.1). Doing it here means every entry point (start / reconnect / swap) mints a
    fresh state; a stale URL from a previous attempt can never be replayed."""
    if auth_class not in _OAUTH_CLASSES:
        return {"requires_client_form": True}

    from app.connections.oauth_flow import UNCONFIGURED_DETAIL, build_authorize_start

    cfg = dict(connection.non_secret_config or {})
    # Swap stages the new provider without cutting over (§3.8.7 rule B) — consent must
    # be sought from the provider the admin is moving TO.
    pending = cfg.get("pending_provider")
    provider = pending if isinstance(pending, str) and pending else connection.provider

    start = build_authorize_start(
        provider=provider,
        admin_id=admin_id,
        instance_id=connection.instance_id,
        connection_id=connection.connection_id,
    )
    cfg["authorize_url"] = start.authorize_url
    cfg["oauth_state_jti"] = start.state_jti
    connection.non_secret_config = cfg
    # The reconnect_pending marker carries the prior status that confirm_reconnect rolls
    # back to; overwriting it would lose the rollback target on a failed probe.
    mid_reconnect = (connection.status_detail or "").startswith("reconnect_pending:")
    if start.unconfigured and not mid_reconnect:
        connection.status_detail = UNCONFIGURED_DETAIL
    return {"authorize_url": start.authorize_url}


def consume_oauth_state(connection: InstanceConnection) -> str | None:
    """Read and CLEAR this connection's pending state ``jti``.

    Clearing is what makes a state single-use: a replayed callback finds no pending
    attempt and is rejected."""
    cfg = dict(connection.non_secret_config or {})
    jti = cfg.pop("oauth_state_jti", None)
    connection.non_secret_config = cfg
    return jti if isinstance(jti, str) else None


async def record_exchange_failure(
    admin_id: uuid.UUID, connection_id: uuid.UUID, detail: str
) -> None:
    """Persist WHY a token exchange failed, in its OWN committed transaction.

    The reason has to outlive the request that discovered it. Callers raise a 409 straight
    afterwards, so writing it to the transaction being abandoned would discard the
    diagnostic along with the rollback, leaving the owner an error toast and a row that
    still claims nothing is wrong."""
    from app.db.session import tenant_session

    async with tenant_session(admin_id) as s:
        conn = (
            await s.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.connection_id == connection_id,
                )
            )
        ).scalar_one_or_none()
        if conn is not None:
            conn.status_detail = f"oauth_exchange_failed: {detail}"


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
    phone_number: str | None = None,
    now: datetime | None = None,
) -> tuple[InstanceConnection, dict[str, object]]:
    """Begin a connect flow: create (or reuse) the row in ``unconfigured`` and return
    the row + StartConnectionResult (§3.8.4 lifecycle starts at unconfigured).

    If a non-revoked row of this type already exists we reuse it (single-active per
    type, §3.8.2) and reset it to unconfigured so the admin can reconfigure.

    BYO number (§3.1.4): for an ``sms_sender`` connect the platform never provisions a
    number — the admin supplies their own E.164 number via ``phone_number``. When
    supplied it is validated and stored as non-secret ``destination`` config and the
    row goes to ``pending_carrier_registration`` (§3.1.6); when omitted the row stays
    ``unconfigured`` — the "Action needed: add your number" state (channel not live)."""
    now = now or datetime.now(UTC)
    # Decision #6: the chosen provider must be one the registry offers for this type.
    # Validated BEFORE any row is written so a bad selection cannot leave a half-created
    # connection behind, and so the provider string can never steer the authorize URL
    # (built from the registry entry) at an endpoint we did not register.
    if registry.has_choice(connection_type) and not registry.is_valid_provider(
        connection_type, provider
    ):
        raise ProviderNotRegistered(
            f"'{provider}' is not an available provider for {connection_type}."
        )
    auth_class = _auth_class_for(connection_type, provider)

    # BYO SMS number intake (§3.1.4): delegate to the single source of truth in
    # phone_lifecycle so the operability gate + honest statuses apply on EVERY attach
    # path (this API path AND any direct caller) — no duplicated status logic, no gap
    # where one path gates and another does not. attach_sms_number validates E.164,
    # probes Twilio operability, and sets pending_carrier_registration (operable) or
    # not_operable_hosting_required (not) with honest detail. It also handles the
    # number-change invalidation (a changed number re-runs the gate from scratch).
    if connection_type == "sms_sender" and phone_number is not None:
        from app.channels.phone_lifecycle import attach_sms_number

        connection = await attach_sms_number(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            e164_number=phone_number,
        )
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
        return connection, _start_result(connection.auth_class, connection, admin_id=admin_id)

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
    return connection, _start_result(auth_class, connection, admin_id=admin_id)


class CredentialRejected(ValueError):
    """The submitted connect-form credential is missing, malformed, or refused."""


# The "add your number" state (§3.1.4): the Twilio account is connected but Luciel has
# no number to send FROM, so the channel is not live and says so.
_SMS_NEEDS_NUMBER = (
    "Action needed: add your number. Your Twilio account is connected — designate the "
    "number Luciel should text from to bring the channel live."
)


async def submit_credentials(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    connection_id: uuid.UUID,
    fields: dict[str, str],
    user_id: uuid.UUID | None,
    store: SecretStore | None = None,
) -> InstanceConnection | None:
    """Complete a credential-form connect: verify, store the secret, connect the row.

    This is the non-OAuth counterpart to the OAuth callback, and it was the missing half
    of the flow — ``requiresClientForm: true`` told the UI to collect fields that had
    nowhere to go. Secret hygiene is the same on both paths (§3.8.3): fields the registry
    marks ``secret`` are handed to the SecretStore under the row's ``secret_ref`` and
    never touch Postgres; the rest land in ``non_secret_config``.

    Verification happens BEFORE the row claims to be connected, so a credential the
    provider would refuse cannot produce a healthy-looking connection."""
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

    spec = registry.get_provider(connection.provider)
    if spec is None or spec.auth_kind != registry.AUTH_CREDENTIAL_FORM:
        raise CredentialRejected(
            f"Provider '{connection.provider}' does not take a credential form."
        )
    known = {f.name: f for f in spec.credential_fields}
    unknown = sorted(set(fields) - set(known))
    if unknown:
        raise CredentialRejected(f"Unknown field(s) for {spec.provider}: {', '.join(unknown)}.")
    missing = sorted(
        f.name for f in spec.credential_fields if f.required and not (fields.get(f.name) or "")
    )
    if missing:
        raise CredentialRejected(f"Missing required field(s): {', '.join(missing)}.")

    if spec.provider == "twilio":
        from app.channels.tenant_twilio import (
            InvalidTwilioCredentials,
            secret_material,
            validate_submitted_credentials,
            verify_credentials,
        )

        try:
            creds = validate_submitted_credentials(fields)
            await verify_credentials(creds)
        except InvalidTwilioCredentials as exc:
            raise CredentialRejected(str(exc)) from exc
        material = secret_material(creds)
        non_secret = {"accountSid": creds.account_sid}
    else:
        material = {n: v for n, v in fields.items() if known[n].secret and v}
        non_secret = {n: v for n, v in fields.items() if not known[n].secret}

    if not connection.secret_ref:
        connection.secret_ref = secret_ref_for(
            admin_id, connection.instance_id, connection.connection_type
        )
    await (store or get_secret_store()).put(connection.secret_ref, material)
    connection.non_secret_config = {**(connection.non_secret_config or {}), **non_secret}

    # A BYO SMS account with no designated number cannot send, so it must not claim to be
    # connected — the channel row shows "add your number" instead (§3.1.4).
    needs_number = connection.connection_type == "sms_sender" and not (
        connection.non_secret_config or {}
    ).get("destination")
    old_status = connection.status
    if needs_number:
        connection.status = "unconfigured"
        connection.status_detail = _SMS_NEEDS_NUMBER
    else:
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
            "old_status": old_status,
            "new_status": connection.status,
            "reason": "credentials_submitted",
        },
    )
    return connection


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
    return connection, _start_result(connection.auth_class, connection, admin_id=admin_id)


async def swap(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    connection_id: uuid.UUID,
    provider: str,
    user_id: uuid.UUID | None,
) -> tuple[InstanceConnection, dict[str, object]] | None:
    """Change the connected account: re-initiate a connect flow against a (possibly
    different) provider WITHOUT taking the current connection offline (§3.8.7 rule B,
    Decision §45 proven-before-cutover).

    A swap is a reconnect that also re-points ``provider`` — but the new provider is
    staged ALONGSIDE the live one, not written in place: ``connection.provider`` is
    left untouched (the EXISTING credential/provider stays authoritative and live)
    and the candidate provider is recorded in ``non_secret_config['pending_provider']``
    (non-secret — §3.8.3 — just a provider name). ``confirm_reconnect()`` is the ONLY
    place ``connection.provider`` is ever written for a swap: on a confirmed-healthy
    probe it flips provider to the staged value; on a failed probe it discards the
    staged value and ``connection.provider`` was never touched, so there is nothing to
    roll back. connection_type is unchanged, so ``auth_class`` (and therefore the
    connect-flow shape) is unchanged. Returns None if no such connection for tenant."""
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

    # Proven-before-cutover: record intent + stage the candidate provider ALONGSIDE
    # the live one (non_secret_config), never mutating connection.provider here. The
    # live status is also preserved via the reconnect_pending marker. Cutover to the
    # new provider AND `connected` happens only when confirm_reconnect health-checks
    # it; on probe failure the staged value is simply discarded (old provider/status
    # were never touched, so "rollback" is a no-op by construction).
    old_status = connection.status
    old_provider = connection.provider
    existing_config = dict(connection.non_secret_config or {})
    existing_config["pending_provider"] = provider
    connection.non_secret_config = existing_config
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
            "reason": "swap_pending",
            "old_provider": old_provider,
            "new_provider": provider,  # staged only — not yet written to `provider`
        },
    )
    return connection, _start_result(connection.auth_class, connection, admin_id=admin_id)


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

    # §3.8.7 rule B: a swap stages its candidate provider alongside the live one in
    # non_secret_config['pending_provider'] rather than mutating `provider` in place
    # (see swap()). Pop it here regardless of outcome so it never lingers as stale
    # config; only apply it to `connection.provider` on a confirmed-healthy probe.
    config = dict(connection.non_secret_config or {})
    pending_provider = config.pop("pending_provider", None)

    if probe_ok:
        # New credential confirmed healthy — cut over now (proven-before-cutover).
        # This is the ONLY place a swap's staged provider becomes authoritative.
        if pending_provider is not None:
            connection.provider = str(pending_provider)
        connection.non_secret_config = config
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
        # The staged pending_provider is simply discarded here; `connection.provider`
        # was never written by swap(), so the old provider is still live untouched.
        connection.non_secret_config = config
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


# --- Uniform lifecycle: DISCONNECT + SWITCH (Decision #5) --------------------------- #
# Connect and reconnect already existed; the owner had no way to hand a connection BACK
# or to move it to a different account. These two verbs complete the lifecycle on the
# SAME surface for every connection type, driven by the provider registry.
#
# `revoke` above is the ACCOUNT-CLOSURE path: the row goes to terminal `revoked` and
# disappears from the live list, which is right when a Luciel is deleted (see
# lifecycle.state_machine) but wrong for "I want to hook up a different calendar" —
# the owner would be left with no row to reconnect and the UI would forget the
# capability existed. `disconnect` is the owner-facing verb: same secret destruction,
# but the row survives in a clean, reconnectable NOT_CONNECTED state.

NOT_CONNECTED = "not_connected"
DISCONNECTED_DETAIL = "Not connected"


async def _load(
    session: AsyncSession, admin_id: uuid.UUID, connection_id: uuid.UUID
) -> InstanceConnection | None:
    return (
        await session.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.connection_id == connection_id,
            )
        )
    ).scalar_one_or_none()


async def _cascade_dependents(
    session: AsyncSession,
    connection: InstanceConnection,
    *,
    admin_id: uuid.UUID,
    user_id: uuid.UUID | None,
) -> dict[str, list[str]]:
    """Disable what depended on this connection, with a reason (Decision #5b).

    Fail-safe: leaving `book_appointment` switched ON after the calendar is gone means
    the owner reads "Appointment scheduling: on" while every dispatch fails the §3.8.7
    gate. So the dependents are switched OFF for real — in BOTH places that hold tool
    state (the relational authorization rows the broker's default-deny gate reads, and
    the Instance JSONB the dashboard reads back), because those two diverging is exactly
    the bug the tools endpoint warns about. The disabled_reason rides on the JSONB entry
    so the UI can say WHY rather than just showing a silently-off toggle.

    Returns {"tools": [...], "channels": [...]} — what was actually turned off."""
    from app.models.instance import Instance
    from app.models.tool_auth import InstanceToolAuthorization

    tool_ids, channel_ids = registry.dependents_of(connection.connection_type)
    reason = f"{connection.connection_type}_disconnected"
    disabled_tools: list[str] = []
    disabled_channels: list[str] = []

    if tool_ids:
        rows = (
            (
                await session.execute(
                    select(InstanceToolAuthorization).where(
                        InstanceToolAuthorization.admin_id == admin_id,
                        InstanceToolAuthorization.instance_id == connection.instance_id,
                        InstanceToolAuthorization.tool_id.in_(tool_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if row.enabled:
                row.enabled = False
                disabled_tools.append(row.tool_id)

    instance = (
        await session.execute(
            select(Instance).where(
                Instance.admin_id == admin_id, Instance.id == connection.instance_id
            )
        )
    ).scalar_one_or_none()
    if instance is not None:
        if tool_ids:
            tools = [dict(t) for t in (instance.tools or [])]
            for entry in tools:
                if entry.get("id") in tool_ids and entry.get("enabled"):
                    entry["enabled"] = False
                    entry["disabledReason"] = reason
                    if entry["id"] not in disabled_tools:
                        disabled_tools.append(str(entry["id"]))
            # Reassign (not mutate in place) so SQLAlchemy sees the JSONB change.
            instance.tools = tools
        if channel_ids:
            channels = [dict(c) for c in (instance.channels or [])]
            for entry in channels:
                if entry.get("id") in channel_ids and entry.get("enabled"):
                    entry["enabled"] = False
                    entry["disabledReason"] = reason
                    disabled_channels.append(str(entry["id"]))
            instance.channels = channels

    await session.flush()
    if disabled_tools or disabled_channels:
        await audit.emit(
            session,
            admin_id=admin_id,
            instance_id=connection.instance_id,
            actor_type=ActorType.admin_user,
            actor_user_id=user_id,
            event_type="instance_configured",
            payload={
                "connection_id": str(connection.connection_id),
                "connection_type": connection.connection_type,
                "action": "dependents_disabled",
                "reason": reason,
                "tools": disabled_tools,
                "channels": disabled_channels,
            },
        )
    return {"tools": disabled_tools, "channels": disabled_channels}


async def disconnect(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    connection_id: uuid.UUID,
    user_id: uuid.UUID | None,
    store: SecretStore | None = None,
) -> dict[str, object] | None:
    """Hand a connection back: destroy the credential, return to a clean, reconnectable
    ``not_connected`` state, and disable whatever depended on it (Decision #5b).

    Credential destruction is the point, so it happens unconditionally and idempotently
    against the SecretStore (§3.6.3) — after this call there is no token on file, and a
    later reconnect starts from a fresh consent. Any half-finished connect attempt is
    swept with it: a parked ``oauth_state_jti`` would otherwise let a callback that is
    still in flight re-credential a connection the owner just disconnected, and a staged
    ``pending_provider`` would silently resurrect on the next confirm.

    Non-secret operational config (a BYO phone number, a provisioned email address) is
    KEPT: it is not credential material, and making the owner retype their own number to
    re-link a calendar-adjacent channel is the "demo, not a product" feeling this pass
    exists to remove. Scope selections are kept for the same reason.

    Returns a summary dict, or None if no such connection for this tenant."""
    store = store or get_secret_store()
    connection = await _load(session, admin_id, connection_id)
    if connection is None:
        return None

    old_status = connection.status
    cfg = dict(connection.non_secret_config or {})
    for ephemeral in ("oauth_state_jti", "authorize_url", "pending_provider"):
        cfg.pop(ephemeral, None)
    connection.non_secret_config = cfg
    connection.status = NOT_CONNECTED
    connection.status_detail = DISCONNECTED_DETAIL
    connection.last_health_check_at = None
    await session.flush()

    deleted = False
    if connection.secret_ref is not None:
        deleted = await store.delete(connection.secret_ref)

    cascaded = await _cascade_dependents(session, connection, admin_id=admin_id, user_id=user_id)

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
            "new_status": NOT_CONNECTED,
            "reason": "disconnect",
            "disabled_tools": cascaded["tools"],
            "disabled_channels": cascaded["channels"],
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
    return {
        "connection": connection,
        "secret_deleted": deleted,
        "disabled_tools": cascaded["tools"],
        "disabled_channels": cascaded["channels"],
    }


async def switch(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    connection_id: uuid.UUID,
    provider: str | None,
    user_id: uuid.UUID | None,
    store: SecretStore | None = None,
) -> tuple[InstanceConnection, dict[str, object]] | None:
    """Move a connection to a different account and/or provider (Decision #5c).

    SWITCH = disconnect + reconnect on one call: the old credential is destroyed first,
    then a fresh connect flow begins against the chosen provider. That ordering is the
    whole point of the verb — the owner asking to switch has ALREADY decided the current
    account is wrong, and several providers only ever issue one active grant per app, so
    holding the old token would either block the new consent or silently keep serving
    from the account the owner is trying to leave.

    This is deliberately a different trade-off from ``swap``, which stages the candidate
    provider alongside a still-live connection (proven-before-cutover, §3.8.7 rule B) and
    is the right verb for re-credentialing an account that currently WORKS. Switch is for
    "use a different account", and it accepts a momentary not-connected window — audited,
    with dependents disabled — rather than serving from the wrong account.

    ``provider`` may be omitted to switch accounts within the SAME provider. When given,
    it must be registered for this connection type (Decision #6) — otherwise an
    admin-supplied string could steer a connect flow at an arbitrary endpoint.

    Returns (connection, StartConnectionResult), or None if not found for this tenant."""
    connection = await _load(session, admin_id, connection_id)
    if connection is None:
        return None

    target = provider or connection.provider
    if not registry.is_valid_provider(connection.connection_type, target):
        raise ProviderNotRegistered(
            f"'{target}' is not an available provider for {connection.connection_type}."
        )

    old_provider = connection.provider
    await disconnect(
        session,
        admin_id=admin_id,
        connection_id=connection_id,
        user_id=user_id,
        store=store,
    )

    spec = registry.get_provider(target)
    connection.provider = target
    if spec is not None:
        connection.auth_class = spec.auth_class
    connection.status = "unconfigured"
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
            "old_status": NOT_CONNECTED,
            "new_status": "unconfigured",
            "reason": "switch",
            "old_provider": old_provider,
            "new_provider": target,
        },
    )
    return connection, _start_result(connection.auth_class, connection, admin_id=admin_id)


# --- Email provisioning (§3.1.6a / Decision #49) ----------------------------------- #
# The tenant provisions the address Luciel sends + receives on. It lives on the
# email_sender connection's non_secret_config as {"address", "mode"} — an additive,
# non-secret passthrough (the same shape family PR #32 used for the SMS phoneNumber).
# own_domain requires a walked-through DNS/MX routing step (status pending_email_routing,
# non-silent precondition, same posture as SMS carrier registration); vm_subdomain is a
# zero-DNS fallback that is immediately connected.

EMAIL_ROUTING_DETAIL = "Action needed: complete email routing"
_VM_SUBDOMAIN_ZONE = "reply.vantagemind.ai"
_INBOUND_MX = "inbound.vantagemind.ai"


def _slugify(name: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug


def vm_subdomain_address(instance_name: str, instance_id: uuid.UUID) -> str:
    """Derive the zero-DNS VantageMind-subdomain address for an instance.

    A full, valid email address (local-part @ per-tenant subdomain) — it is used as
    both the SES sender identity and the inbound recipient resolution key, so it must
    be a real address, not a bare domain."""
    slug = _slugify(instance_name) or instance_id.hex[:12]
    return f"luciel@{slug}.{_VM_SUBDOMAIN_ZONE}"


def dns_records_for(address: str) -> list[dict[str, object]]:
    """The DNS records an own-domain admin must add to complete inbound routing.
    Derived deterministically from the address domain; carries NO secret material."""
    domain = address.split("@", 1)[1] if "@" in address else address
    return [
        {"type": "MX", "host": domain, "value": _INBOUND_MX, "priority": 10},
        {"type": "TXT", "host": domain, "value": "v=spf1 include:mail.vantagemind.ai ~all"},
        {"type": "CNAME", "host": f"vm._domainkey.{domain}", "value": "dkim.vantagemind.ai"},
    ]


async def provision_email(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    mode: str,
    email_address: str | None,
    instance_name: str,
    user_id: uuid.UUID | None,
    now: datetime | None = None,
) -> InstanceConnection:
    """Provision the tenant's email sender identity (§3.1.6a / Decision #49).

    own_domain — stores the admin-supplied address; status = pending_email_routing
      until the inbound DNS/MX routing is verified (non-silent precondition).
    vm_subdomain — derives a zero-DNS subdomain address; immediately connected.

    Reuses the single-active email_sender row (§3.8.2). The address + mode are stored
    in non_secret_config; NO secret material is written here (§3.8.3)."""
    now = now or datetime.now(UTC)
    if mode == "own_domain":
        address = (email_address or "").strip()
        if not address:
            raise ValueError("own_domain requires an email address")
        status = "pending_email_routing"
        status_detail: str | None = EMAIL_ROUTING_DETAIL
    else:  # vm_subdomain
        address = vm_subdomain_address(instance_name, instance_id)
        status = "connected"
        status_detail = None

    existing = await _single_active(session, admin_id, instance_id, "email_sender")
    if existing is not None:
        connection = existing
        connection.provider = "ses"
        connection.auth_class = _auth_class_for("email_sender")
    else:
        connection = InstanceConnection(
            connection_id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            connection_type="email_sender",
            auth_class=_auth_class_for("email_sender"),
            provider="ses",
            secret_ref=secret_ref_for(admin_id, instance_id, "email_sender"),
            created_by_user_id=user_id,
        )
        session.add(connection)
    connection.status = status
    connection.status_detail = status_detail
    connection.non_secret_config = {"address": address, "mode": mode}
    await session.flush()

    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        actor_type=ActorType.admin_user,
        actor_user_id=user_id,
        event_type="instance_configured",
        payload={
            "connection_id": str(connection.connection_id),
            "connection_type": "email_sender",
            "action": "provision_email",
            "mode": mode,
            "status": status,
            # §3.8.3: the address is non-secret config; safe to audit.
            "address": address,
        },
    )
    return connection


async def get_email_provisioning(
    session: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID
) -> InstanceConnection | None:
    """The current (non-revoked) email_sender connection, if provisioned."""
    return await _single_active(session, admin_id, instance_id, "email_sender")


_DETAIL_ROUTING_CONFIRMED = "Email routing is confirmed — mail to this address will reach Luciel."
_DETAIL_ROUTING_NOT_YET = (
    "We still don't see the MX record pointing at us. Add the DNS records shown below "
    "at your domain registrar, then click Re-verify — DNS changes can take a little "
    "while to propagate."
)
_DETAIL_ROUTING_UNKNOWN = (
    "Couldn't look up this domain's DNS just now. It has not been marked routed; "
    "click Re-verify to try again."
)


async def reverify_email_routing(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    probe: MxDelegationProbe | None = None,
) -> InstanceConnection | None:
    """Re-run the MX-delegation probe against the tenant's CURRENTLY provisioned
    own_domain address (§3.1.6a). Mirrors ``phone_lifecycle.reverify_sms_number``
    (harmony fix 2026-08-04 item 5): stateless + repeatable (no background poller, no
    expiry) — the tenant clicks this after adding the MX record at their registrar.
    Transitions to ``connected`` only on a confirmed live DELEGATED result; a domain
    that still doesn't resolve, or resolves without our MX, stays in the honest
    ``pending_email_routing`` state with an actionable detail. Reads the current
    address live, so a since-changed address is what gets checked.

    Returns the updated row, ``None`` if no email_sender connection exists yet, or
    unchanged (still ``pending_email_routing``, detail refreshed) if the connection is
    ``vm_subdomain`` mode — that mode has no DNS step and is never pending, so there is
    nothing to re-verify."""
    from app.channels.email_routing import build_mx_probe

    existing = await _single_active(session, admin_id, instance_id, "email_sender")
    if existing is None:
        return None
    cfg = existing.non_secret_config or {}
    if cfg.get("mode") != "own_domain":
        return existing
    address = str(cfg.get("address") or "")
    domain = address.split("@", 1)[1] if "@" in address else ""
    if not domain:
        return existing

    probe = probe or build_mx_probe()
    result = await probe.check(domain, _INBOUND_MX)
    if result is MxDelegationResult.DELEGATED:
        existing.status = "connected"
        existing.status_detail = _DETAIL_ROUTING_CONFIRMED
    elif result is MxDelegationResult.NOT_DELEGATED:
        existing.status = "pending_email_routing"
        existing.status_detail = _DETAIL_ROUTING_NOT_YET
    else:
        existing.status = "pending_email_routing"
        existing.status_detail = _DETAIL_ROUTING_UNKNOWN
    await session.flush()
    return existing


async def resolve_email_address(
    session: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID
) -> str | None:
    """The tenant's provisioned send+receive address, or None if not provisioned.

    This is the authoritative per-tenant email identity (§3.1.6a) — the EmailAdapter
    default is only a last-resort fallback, and the prompt composer uses this to tell
    Luciel its own address."""
    conn = await _single_active(session, admin_id, instance_id, "email_sender")
    if conn is None or not conn.non_secret_config:
        return None
    address = conn.non_secret_config.get("address")
    return address if isinstance(address, str) else None


async def _resolve_destination(
    session: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID, connection_type: str
) -> str | None:
    conn = await _single_active(session, admin_id, instance_id, connection_type)
    if conn is None or not conn.non_secret_config:
        return None
    destination = conn.non_secret_config.get("destination")
    return destination if isinstance(destination, str) and destination else None


async def resolve_sms_number(
    session: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID
) -> str | None:
    """The tenant's provisioned SMS sending number (E.164), or None (§3.1.4 BYO).

    This is the FROM identity for every outbound SMS on the tenant's behalf — a customer
    reply and an escalation page both have to come from the tenant's own number."""
    return await _resolve_destination(session, admin_id, instance_id, "sms_sender")


# Which connection + destination key(s) a runtime channel may bind, in the order tried.
# Since the §3.1.2 split each messaging channel is its own id with exactly one grant:
# WhatsApp and Messenger live on the Facebook ``channel_auth`` grant (each under its own
# destination key), Instagram on the separate Business Login ``instagram_auth`` grant.
# The legacy combined ``instagram_messenger`` id (sessions opened before the split) still
# spans both connections so an old in-flight conversation can keep replying.
INSTAGRAM_CONNECTION_TYPE = "instagram_auth"
META_CHANNEL_DESTINATION_KEYS: dict[str, tuple[tuple[str, str], ...]] = {
    "whatsapp": (("channel_auth", "whatsapp"),),
    "messenger": (("channel_auth", "messenger"),),
    "instagram": ((INSTAGRAM_CONNECTION_TYPE, "instagram"),),
    "instagram_messenger": (
        (INSTAGRAM_CONNECTION_TYPE, "instagram"),
        ("channel_auth", "messenger"),
    ),
}


async def resolve_meta_destination(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    channel: str | None = None,
) -> str | None:
    """The tenant's Meta/Instagram sender id for ``channel``, or None if not bound.

    For WhatsApp this is the WhatsApp phone-number id; for Instagram DM / Messenger it is
    the IG-account / Facebook-page id (§3.1.3 — these channels bind to a page/account, not
    a phone number). Each channel names its own asset under
    ``non_secret_config['destinations']`` on its grant's connection, so binding Messenger
    never unbinds WhatsApp. Falls back to the single legacy ``destination`` on the Facebook
    connection for rows bound before per-channel destinations existed.
    """
    for connection_type, key in META_CHANNEL_DESTINATION_KEYS.get(channel or "", ()):
        conn = await _single_active(session, admin_id, instance_id, connection_type)
        destinations = (conn.non_secret_config or {}).get("destinations") if conn else None
        value = destinations.get(key) if isinstance(destinations, dict) else None
        if isinstance(value, str) and value:
            return value

    conn = await _single_active(session, admin_id, instance_id, "channel_auth")
    legacy = (conn.non_secret_config or {}).get("destination") if conn else None
    return legacy if isinstance(legacy, str) and legacy else None


async def meta_grant_for_destination(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    channel: str,
    destination: str,
) -> str:
    """Which connection holds the token allowed to send AS ``destination``.

    ``instagram_messenger`` spans two grants, and the asset decides which one: an IG
    account id is only sendable with the Instagram Login token and a Page id only with the
    Facebook one. Pairing them by asset rather than by channel is what stops a Messenger
    reply going out on the Instagram grant (and being rejected) for a tenant who connected
    both. Unbound assets fall back to the Facebook grant, which is where the legacy
    single-``destination`` rows live.
    """
    for connection_type, key in META_CHANNEL_DESTINATION_KEYS.get(channel, ()):
        conn = await _single_active(session, admin_id, instance_id, connection_type)
        destinations = (conn.non_secret_config or {}).get("destinations") if conn else None
        if isinstance(destinations, dict) and destinations.get(key) == destination:
            return connection_type
    return "channel_auth"
