"""Health-check + token-refresh worker (Architecture §3.8.5).

Runs per connection in ``instance_connections`` and applies UNIFORMLY to every
connection type — tool, channel, and knowledge-sync alike ("a connection is a
connection", §3.8.5). It:
  * health-probes the connection on a cadence keyed to ``auth_class`` (NOT provider
    brand, §3.8.5) — so a new provider inherits the right behavior automatically;
  * proactively refreshes OAuth/long-lived tokens ~30 min before expiry (§3.8.5
    refresh path); on refresh failure → status='expired' + status_detail + audit;
  * transitions status (connected / error / expired) and stamps
    ``last_health_check_at`` + ``status_detail`` (§3.8.4 lifecycle);
  * for a revoked/expired knowledge_source connection, pauses the sync and flags
    the source paused_reconnect_needed — reusing the Arc 11b mechanism
    (app/knowledge/sync.pause_sync_on_revocation, §3.2.3) rather than a parallel one.

``dormant`` (§3.8.4 / Legal §A3) is a BILLING state, not a health state: dunning
suspends action-tool connections when payment is lost (app/billing/dunning.py)
and the payment webhooks restore them. The sweep therefore leaves dormant rows
untouched — probing one and finding it healthy must NOT flip it back to
``connected``, or the 6-hour sweep would silently re-arm a non-paying account's
action tools. (An earlier draft also set dormant here on a 30-day unused window;
that branch was provably unreachable — it compared against
``last_health_check_at``, which every sweep pass re-stamps — and has been
removed rather than left looking load-bearing.)

Both the health PROBE and the token REFRESH are behind swappable Protocol seams, so
tests drive them deterministically with no provider call. The implementations the
SCHEDULED sweep binds are ``StoredCredentialProbe`` and ``ReconnectOnExpiry`` below;
neither talks to a provider either, and each says plainly what it does and does not
prove rather than reporting an unverified green.

``run_health_sweep`` is the one-shot pass. Its caller is the ``health`` sweep in
``app/jobs/sweeps.py`` (P1-8) — before that it had none, which meant the status the
universal gate consults was last computed the day the admin connected.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.connections.secrets import SecretStore, get_secret_store
from app.knowledge import sync as knowledge_sync
from app.models.audit import ActorType
from app.models.connection import InstanceConnection

_log = logging.getLogger(__name__)

# §3.8.5 TARGET cadence per auth_class. Honesty note (round 5B): the deployed
# scheduler runs ONE 6-hour EventBridge cron for every class — this table is the
# design target a per-class scheduler would read, not a description of what runs
# today. Kept because the sweep entrypoint is cadence-agnostic either way.
HEALTH_CADENCE: dict[str, timedelta] = {
    "oauth_token": timedelta(minutes=15),
    "long_lived_token": timedelta(minutes=60),
    "api_key": timedelta(minutes=60),
    "provisioned_resource": timedelta(hours=4),
}
# §3.8.5 refresh path: refresh proactively ~30 min before known expiry.
REFRESH_LEAD = timedelta(minutes=30)

# Statuses the health worker OWNS and may transition between (§3.8.4/§3.8.5 —
# round 5B item 10). Everything else is SETUP-OWNED: the connect flows, billing,
# and lifecycle own unconfigured / not_connected / pending_carrier_registration /
# pending_email_routing / not_operable_hosting_required / dormant / revoked, and
# the sweep must never touch them. Live-caught 2026-08-24: the old denylist
# (revoked, dormant) let one sweep promote a not-yet-operable number to a false
# Connected, erase setup guidance out of status_detail, and flip never-consented
# rows to a false "error" with a false attention email.
HEALTH_OWNED_STATUSES: frozenset[str] = frozenset({"connected", "expired", "error"})


def health_owned(connection: InstanceConnection) -> bool:
    """True iff the health worker may transition this row (§3.8.4 taxonomy).

    A row mid staged-swap (``reconnect_pending:`` marker, service.py rule B) is
    setup-owned regardless of status — transitioning it would erase the rollback
    target ``confirm_reconnect`` needs."""
    if connection.status not in HEALTH_OWNED_STATUSES:
        return False
    return not (connection.status_detail or "").startswith("reconnect_pending:")


@dataclass
class ProbeResult:
    """Outcome of a provider health probe (§3.8.5)."""

    ok: bool
    # Optional detail surfaced to the admin on error/expired (never a secret, §3.8.3).
    detail: str | None = None


class ConnectionProbe(Protocol):
    """Provider-appropriate liveness probe (§3.8.5). Deterministic in tests; the real
    provider HTTP probe binds later behind this same seam (AWS/provider-DEFERRED)."""

    async def probe(self, connection: InstanceConnection) -> ProbeResult: ...


class TokenRefresher(Protocol):
    """Refreshes an OAuth/long-lived token near expiry (§3.8.5 refresh path).

    Returns the new expiry on success, or None to signal refresh failure (→ expired).
    Deterministic in tests; the real provider token-endpoint call + Secrets-Manager
    write binds later behind this seam (AWS/provider-DEFERRED)."""

    async def refresh(
        self, connection: InstanceConnection, store: SecretStore
    ) -> datetime | None: ...


class StoredCredentialProbe:
    """The scheduled sweep's probe: is the credential this connection points at still there?

    This is deliberately NOT a provider liveness call — that seam is still deferred, and
    binding it would mean one outbound request per connection per sweep against six
    different providers' rate limits. What it checks instead is the failure the sweep
    exists to catch and that we can answer authoritatively: a connection whose
    ``secret_ref`` no longer resolves. That happens after an out-of-band Secrets Manager
    deletion or a half-finished disconnect, and until now it stayed ``connected``
    forever, so every use of it failed one at a time in front of a customer.

    A connection with no ``secret_ref`` (``provisioned_resource`` — the platform owns the
    resource, there is no tenant credential) has nothing to look up and passes.

    The honest boundary: passing means "we still hold a credential", not "the provider
    accepted it". It never reports a green it has not verified — it just verifies less
    than the eventual probe will.
    """

    def __init__(self, store: SecretStore | None = None) -> None:
        self._store = store

    async def probe(self, connection: InstanceConnection) -> ProbeResult:
        # Round 5B item 11: only report a loss the probe can PROVE. Rows whose
        # shape legitimately holds no secret — legacy platform ses rows, a
        # credential form whose only fields were non-secret (custom_webhook with
        # just a URL), providers with no stored material contract — used to flip
        # to a false "error — stored credential is missing" with a false
        # attention email (live-caught 2026-08-24 on the dev tenant's webhook CRM).
        from app.connections import registry  # noqa: PLC0415 — avoids an import cycle

        if not registry.secret_material_expected(connection.provider, connection.auth_class):
            return ProbeResult(ok=True)
        if not connection.secret_ref:
            return ProbeResult(ok=True)
        store = self._store or get_secret_store()
        try:
            material = await store.get(connection.secret_ref)
        except Exception as exc:  # noqa: BLE001
            # A store outage is not evidence the tenant's credential is gone. Marking
            # every connection on the platform ``error`` because Secrets Manager blinked
            # would be a self-inflicted outage, so leave the status alone.
            return ProbeResult(ok=True, detail=f"probe_skipped:{type(exc).__name__}")
        if not material:
            return ProbeResult(ok=False, detail="stored credential is missing — reconnect")
        return ProbeResult(ok=True)


class ReconnectOnExpiry:
    """The scheduled sweep's refresher: there isn't one, and it says so.

    No provider token endpoint is bound yet, so there is no way to rotate an expiring
    OAuth token. The choice is between reporting the connection as fine until it dies
    mid-conversation, or telling the admin 30 minutes early that it needs reconnecting.
    Returning None takes the second: ``check_connection`` transitions the connection to
    ``expired`` with "reconnect in dashboard", pauses dependent knowledge sync, and
    audits the change — an actionable warning instead of a surprise.
    """

    async def refresh(self, connection: InstanceConnection, store: SecretStore) -> datetime | None:
        return None


def _expiry_of(connection: InstanceConnection) -> datetime | None:
    """Read the non-secret token-expiry hint from non_secret_config (§3.8.3: the
    expiry timestamp is non-secret metadata; the token itself is in the SecretStore)."""
    cfg = connection.non_secret_config or {}
    raw = cfg.get("token_expires_at")
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _supports_refresh(auth_class: str) -> bool:
    # api_key / provisioned_resource don't auto-refresh (§3.8.5 table).
    return auth_class in ("oauth_token", "long_lived_token")


async def check_connection(
    session: AsyncSession,
    *,
    connection: InstanceConnection,
    probe: ConnectionProbe,
    refresher: TokenRefresher,
    store: SecretStore,
    now: datetime | None = None,
) -> str:
    """Health-check + refresh ONE connection; return the resulting status (§3.8.5).

    Order: (1) proactively refresh if near expiry and the auth class supports it —
    on refresh failure go expired immediately (don't probe a dead token). (2) Probe
    liveness. Always stamps last_health_check_at; emits connection_status_changed
    on a real transition. ``dormant`` rows never reach here (the sweep excludes
    them — billing owns that state, module docstring), and setup-owned rows
    (``health_owned`` False) are returned untouched even when a caller hands one
    in directly — no stamp, no transition, no email (round 5B item 10)."""
    now = now or datetime.now(UTC)
    if not health_owned(connection):
        return connection.status
    old_status = connection.status

    # (1) Proactive token refresh ahead of expiry (§3.8.5 refresh path).
    if _supports_refresh(connection.auth_class):
        expiry = _expiry_of(connection)
        if expiry is not None and now >= expiry - REFRESH_LEAD:
            new_expiry = await refresher.refresh(connection, store)
            if new_expiry is None:
                # Refresh failed → expired + reconnect detail (§3.8.5 step 5).
                await _transition(
                    session,
                    connection,
                    "expired",
                    "refresh failed — reconnect in dashboard",
                    now,
                    old_status,
                )
                await _propagate_knowledge_pause(session, connection)
                return connection.status
            # Success → persist new expiry hint (non-secret); stays connected (§3.8.5 step 4).
            cfg = dict(connection.non_secret_config or {})
            cfg["token_expires_at"] = new_expiry.isoformat()
            connection.non_secret_config = cfg

    # (2) Provider liveness probe.
    result = await probe.probe(connection)
    if not result.ok:
        await _transition(
            session, connection, "error", result.detail or "health check failed", now, old_status
        )
        await _propagate_knowledge_pause(session, connection)
        return connection.status

    # (3) Healthy → connected.
    await _transition(session, connection, "connected", None, now, old_status)
    return connection.status


async def mark_connection_broken(
    session: AsyncSession,
    connection: InstanceConnection,
    *,
    detail: str,
    status: str = "error",
    now: datetime | None = None,
) -> None:
    """Flip a connection into a broken health status through the SAME transition
    machinery the sweep uses — audit + once-per-incident attention email (round
    5B item 12: a live-call failure discovered outside the sweep, e.g. a
    knowledge sync whose token the provider rejected, must surface exactly like
    a sweep-detected one instead of pausing silently)."""
    await _transition(
        session, connection, status, detail, now or datetime.now(UTC), connection.status
    )


async def _transition(
    session: AsyncSession,
    connection: InstanceConnection,
    new_status: str,
    detail: str | None,
    now: datetime,
    old_status: str,
) -> None:
    """Apply the status change, stamp the health check, and audit a real transition."""
    connection.status = new_status
    connection.status_detail = detail
    connection.last_health_check_at = now
    if new_status != old_status:
        # §3.8.4: status changes → admin_audit_log connection_status_changed with
        # old/new status + reason. NEVER any secret material (§3.8.3 rule 4).
        await audit.emit(
            session,
            admin_id=connection.admin_id,
            instance_id=connection.instance_id,
            actor_type=ActorType.platform,
            event_type="connection_status_changed",
            payload={
                "connection_id": str(connection.connection_id),
                "old_status": old_status,
                "new_status": new_status,
                "reason": detail or "health_check",
            },
        )
        # §3.8.5's second half (Phase 6.6): the TRANSITION into a broken state is
        # the once-per-incident boundary — a still-broken row on the next sweep
        # does not transition and sends nothing; a fixed-then-broken-again row
        # transitions again and notifies again. Best-effort: the dispatcher
        # swallows delivery errors (audited), so a mail outage cannot break the
        # health sweep.
        broken = ("expired", "error")
        if new_status in broken and old_status not in broken:
            await _notify_owner_attention(session, connection, new_status, detail)


# Owner-facing surface names for the attention email (never raw enums).
_SURFACE_LABELS: dict[str, str] = {
    "calendar": "calendar",
    "crm": "CRM",
    "knowledge_source": "knowledge sync",
    "sms_sender": "SMS & Voice number",
    "email_sender": "email mailbox",
    "channel_auth": "WhatsApp & Messenger sign-in",
    "instagram_auth": "Instagram sign-in",
    "outbound_webhook": "outbound webhook",
    "record_source": "records source",
}


async def _notify_owner_attention(
    session: AsyncSession,
    connection: InstanceConnection,
    new_status: str,
    detail: str | None,
) -> None:
    """Email the owner that this connection needs a reconnect (§3.8.5)."""
    from sqlalchemy import select as _select  # noqa: PLC0415

    from app.connections import registry  # noqa: PLC0415
    from app.models.instance import Account  # noqa: PLC0415
    from app.notifications import email_dispatcher  # noqa: PLC0415

    email = (
        await session.execute(_select(Account.email).where(Account.id == connection.admin_id))
    ).scalar_one_or_none()
    if not email:
        return
    spec = registry.get_provider(connection.provider)
    provider_name = spec.display_name if spec is not None else connection.provider
    await email_dispatcher.send_connection_attention(
        session,
        admin_id=connection.admin_id,
        to=email,
        provider_name=provider_name,
        surface_label=_SURFACE_LABELS.get(connection.connection_type, "connection"),
        status=new_status,
        detail=detail,
    )


async def _propagate_knowledge_pause(session: AsyncSession, connection: InstanceConnection) -> None:
    """Expired/error knowledge_source connection → pause sync + paused_reconnect_needed.

    Reuses the Arc 11b mechanism (§3.2.3) rather than a parallel one (§3.8 single
    abstraction): a revoked/expired token must not let Luciel serve stale content."""
    if connection.connection_type != "knowledge_source":
        return
    await knowledge_sync.pause_sync_on_revocation(
        session, admin_id=connection.admin_id, connection_id=connection.connection_id
    )


async def run_health_sweep(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    probe: ConnectionProbe,
    refresher: TokenRefresher,
    store: SecretStore | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """One-shot sweep over every non-revoked connection for a tenant (§3.8.5).

    Returns {connection_id: resulting_status} for tests/observability. A real
    background worker calls this on the auth-class cadence; CI calls it directly as a
    one-shot so no live loop is required."""
    store = store or get_secret_store()
    now = now or datetime.now(UTC)
    connections = (
        (
            await session.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin_id,
                    # POSITIVE allowlist (round 5B item 10): the sweep owns only
                    # the health statuses. The old denylist (revoked, dormant)
                    # swept up every SETUP-owned status too — promoting
                    # not-yet-operable numbers to a false Connected, erasing
                    # setup guidance, and false-erroring never-consented rows
                    # (live-caught 2026-08-24). revoked stays terminal; dormant
                    # is billing-suspended (Legal §A3) and only the payment
                    # webhooks may leave it; a future unknown status is now safe
                    # by default. The staged-swap marker is guarded per-row in
                    # health_owned (it rides status_detail, not status).
                    InstanceConnection.status.in_(HEALTH_OWNED_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    out: dict[str, str] = {}
    for connection in connections:
        status = await check_connection(
            session,
            connection=connection,
            probe=probe,
            refresher=refresher,
            store=store,
            now=now,
        )
        out[str(connection.connection_id)] = status
    return out


# --------------------------------------------------------------------------- #
# Round 6 WP-C: a live call the provider refused flips the row at once
# --------------------------------------------------------------------------- #

#: Failure-reason suffixes the tool backends use when the provider answered 401/403.
PROVIDER_AUTH_FAILURE_SUFFIXES = ("_http_401", "_http_403")


def is_provider_auth_failure(reason: str) -> bool:
    """True when a backend's failure reason says the provider refused the credential."""
    return reason.endswith(PROVIDER_AUTH_FAILURE_SUFFIXES)


async def flag_provider_auth_failure(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    connection_type: str,
    reason: str,
    connection_id: uuid.UUID | None = None,
) -> bool:
    """A provider refused the stored credential on a LIVE call: flip the row to
    ``error`` through the sweep's own transition — audit row, one attention email —
    right now, instead of letting it read Connected until the next sweep while every
    push or booking fails in front of a customer (round 6 WP-C, F081). Health-owned
    rows only; a row mid-swap or setup-owned is left to its owner. Returns whether a
    row was flipped."""
    stmt = select(InstanceConnection).where(
        InstanceConnection.admin_id == admin_id,
        InstanceConnection.instance_id == instance_id,
        InstanceConnection.connection_type == connection_type,
        InstanceConnection.status != "revoked",
    )
    if connection_id is not None:
        stmt = stmt.where(InstanceConnection.connection_id == connection_id)
    row = (
        (await session.execute(stmt.order_by(InstanceConnection.created_at.desc())))
        .scalars()
        .first()
    )
    if row is None or not health_owned(row):
        return False
    from app.connections import registry  # noqa: PLC0415 — avoids an import cycle

    spec = registry.get_provider(row.provider)
    name = spec.display_name if spec is not None else row.provider
    await mark_connection_broken(
        session, row, detail=f"{name} rejected the stored credential ({reason}) — reconnect"
    )
    return True


async def flag_if_provider_auth_failure(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    connection_type: str,
    reason: str,
) -> bool:
    """``flag_provider_auth_failure`` when ``reason`` is a 401/403, else nothing. Never
    raises: the flag rides a customer's turn and must not turn one failure into two."""
    if not is_provider_auth_failure(reason):
        return False
    try:
        return await flag_provider_auth_failure(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            connection_type=connection_type,
            reason=reason,
        )
    except Exception:  # noqa: BLE001 — the flag is best-effort; the call already failed
        _log.exception("provider auth failure could not be flagged type=%s", connection_type)
        return False
