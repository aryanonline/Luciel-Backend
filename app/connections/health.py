"""Health-check + token-refresh worker (Architecture §3.8.5).

Runs per connection in ``instance_connections`` and applies UNIFORMLY to every
connection type — tool, channel, and knowledge-sync alike ("a connection is a
connection", §3.8.5). It:
  * health-probes the connection on a cadence keyed to ``auth_class`` (NOT provider
    brand, §3.8.5) — so a new provider inherits the right behavior automatically;
  * proactively refreshes OAuth/long-lived tokens ~30 min before expiry (§3.8.5
    refresh path); on refresh failure → status='expired' + status_detail + audit;
  * transitions status (connected / error / expired / dormant) and stamps
    ``last_health_check_at`` + ``status_detail`` (§3.8.4 lifecycle);
  * for a revoked/expired knowledge_source connection, pauses the sync and flags
    the source paused_reconnect_needed — reusing the Arc 11b mechanism
    (app/knowledge/sync.pause_sync_on_revocation, §3.2.3) rather than a parallel one.

``dormant`` (§3.8.4): a connection preserved but unused for a long window — defined
here as healthy yet no successful use within ``DORMANT_AFTER``. It is not dispatched
by the broker and is restored on re-use/re-upgrade. (The downgrade-grace meaning in
§3.8.4 is the lifecycle side; this worker sets the unused-window side.)

Both the health PROBE and the token REFRESH are behind swappable Protocol seams
with DETERMINISTIC test impls — NO real provider/OAuth/Secrets-Manager call in this
arc or in CI (AWS/provider-DEFERRED SEAM, binds on explicit owner approval).

Invocable as a one-shot sweep (``run_health_sweep``) for tests; a real background
worker just calls this on the auth-class cadence — no live loop needed in CI.
"""

from __future__ import annotations

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

# §3.8.5 cadence keyed to auth_class (minutes). The worker reads auth_class, never
# the provider brand, so the catalog can grow without touching this table.
HEALTH_CADENCE: dict[str, timedelta] = {
    "oauth_token": timedelta(minutes=15),
    "long_lived_token": timedelta(minutes=60),
    "api_key": timedelta(minutes=60),
    "provisioned_resource": timedelta(hours=4),
}
# §3.8.5 refresh path: refresh proactively ~30 min before known expiry.
REFRESH_LEAD = timedelta(minutes=30)
# Unused-window after which a healthy connection is marked dormant (§3.8.4). 30 days
# matches the downgrade-grace horizon used elsewhere in the lifecycle (§3.6).
DORMANT_AFTER = timedelta(days=30)


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
    liveness. (3) Apply dormant if healthy-but-unused. Always stamps
    last_health_check_at; emits connection_status_changed on a real transition."""
    now = now or datetime.now(UTC)
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

    # (3) Healthy. Dormant if unused for a long window (§3.8.4), else connected.
    last_used = connection.last_health_check_at
    if old_status == "connected" and last_used is not None and now - last_used >= DORMANT_AFTER:
        await _transition(session, connection, "dormant", "unused > 30 days", now, old_status)
    else:
        await _transition(session, connection, "connected", None, now, old_status)
    return connection.status


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
                    InstanceConnection.status != "revoked",
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
