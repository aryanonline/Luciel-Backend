"""Scheduled knowledge sync — the periodic half of "live sync" (§3.2.3).

``sync.sync_connection`` (delta reconcile: add/update/remove) existed but was
reachable only from the two MANUAL dashboard routes, so a "kept in sync" source
was in truth synced exactly as often as the owner remembered to click Re-sync —
and a crawled website was never re-crawled at all. This module is the sweep-side
driver: for one tenant, sync every CONNECTED knowledge connection whose sources
are due under the per-origin cadence.

Cadence is per ORIGIN CLASS, honest about cost (§3.2.3): API-delta providers
(Drive / Notion / CRM-KB) are cheap and run every 6 hours; website crawl is a
full re-fetch + diff — the expensive, feed-less one — and runs every 24 hours.
Manual re-sync from the dashboard remains available for immediacy.

A ``ProviderUnavailable`` (revoked/expired credential) pauses the connection's
sources via the existing §3.2.3 mechanism — the sweep never lets a dead
credential silently serve stale content, and never crashes the tenant loop.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connections.secrets import get_secret_store
from app.knowledge import service, sync
from app.knowledge.providers import ProviderUnavailable, provider_for_connection
from app.models.connection import InstanceConnection
from app.models.knowledge import KnowledgeSource

logger = logging.getLogger(__name__)

# provider (brand) -> source_origin (§3.2.3 wire vocabulary). Mirrors the map the
# manual routes use (app/api/v1/admin/knowledge.py) — hubspot/salesforce are one
# KB origin, two native connectors.
PROVIDER_TO_ORIGIN: dict[str, str] = {
    "google_drive": "google_drive",
    "notion": "notion",
    "hubspot": "crm_kb",
    "salesforce": "crm_kb",
    "website_crawl": "website_crawl",
}

# §3.2.3 sync cadence: platform-tuned, not admin-configurable.
CRAWL_CADENCE = timedelta(hours=24)
DELTA_CADENCE = timedelta(hours=6)


def _cadence_for(provider: str) -> timedelta:
    return CRAWL_CADENCE if provider == "website_crawl" else DELTA_CADENCE


async def sync_due_connections(
    session: AsyncSession, *, admin_id: uuid.UUID, now: datetime | None = None
) -> int:
    """Sync every due, CONNECTED knowledge connection for one tenant.

    Due-ness is the newest ``last_synced_at`` across the connection's live
    sources: absent (never synced through the sweep) counts as due. Returns the
    number of connections synced (the sweep log's "did anything happen").
    """
    now = now or datetime.now(UTC)
    connections = (
        (
            await session.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.connection_type == "knowledge_source",
                    InstanceConnection.status == "connected",
                )
            )
        )
        .scalars()
        .all()
    )
    synced = 0
    for conn in connections:
        origin = PROVIDER_TO_ORIGIN.get(conn.provider)
        if origin is None:
            continue  # a provider this sweep does not know how to schedule
        newest = (
            await session.execute(
                select(func.max(KnowledgeSource.last_synced_at)).where(
                    KnowledgeSource.admin_id == admin_id,
                    KnowledgeSource.connection_id == conn.connection_id,
                    KnowledgeSource.soft_deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if newest is not None:
            newest = newest if newest.tzinfo else newest.replace(tzinfo=UTC)
            if now - newest < _cadence_for(conn.provider):
                continue  # fresh enough under the origin's cadence
        try:
            # Round 5B item 12: refresh the token at CALL time like every other
            # live consumer (calendar/CRM/outlook do this). Google tokens live
            # ~1h; the sync only worked because the schedule happened to run ten
            # minutes after the health sweep, and the manual Re-sync button had
            # no such luck. The refreshed token is persisted, so the provider's
            # store read below picks it up.
            from app.connections.refresh import ensure_fresh_access_token  # noqa: PLC0415

            await ensure_fresh_access_token(
                session,
                admin_id,
                conn.instance_id,
                connection_type="knowledge_source",
                connection_id=conn.connection_id,
            )
            await sync.sync_connection(
                session,
                admin_id=admin_id,
                instance_id=conn.instance_id,
                connection_id=conn.connection_id,
                origin=origin,
                provider=provider_for_connection(conn, get_secret_store()),
            )
            synced += 1
        except ProviderUnavailable as exc:
            # Dead credential: pause + flag (§3.2.3) — never silently serve
            # stale content, never crash the rest of the tenant's connections.
            # Round 5B item 12: the BACKING connection flips through the health
            # transition machinery too (audit + once-per-incident attention
            # email + the dashboard attention counter) — a broken sync used to
            # pause with no notification anywhere.
            await sync.pause_sync_on_revocation(
                session, admin_id=admin_id, connection_id=conn.connection_id
            )
            from app.connections.health import mark_connection_broken  # noqa: PLC0415

            await mark_connection_broken(
                session,
                conn,
                detail="knowledge sync could not use the stored credential — reconnect",
            )
            logger.info(
                "knowledge_sync_paused provider=%s error=%s", conn.provider, type(exc).__name__
            )
        except Exception as exc:  # noqa: BLE001 — one bad source must not stop the sweep
            # Non-credential failure (quota, parse, provider 500): the sources
            # show the honest "error" sync state instead of a quietly aging
            # "synced" (round 5B item 12); the connection row itself stays as-is
            # (the credential is not implicated).
            await sync.mark_sync_error(session, admin_id=admin_id, connection_id=conn.connection_id)
            logger.warning(
                "knowledge_sync_failed provider=%s error=%s", conn.provider, type(exc).__name__
            )
    # A changed embedding pin (F121): bring this tenant's older vectors forward in a
    # bounded batch per pass, so retrieval never scores a new-model query against
    # old-model chunks for longer than a few sweeps.
    try:
        reembedded = await service.reembed_outdated_chunks(session, admin_id=admin_id)
        if reembedded:
            logger.info("knowledge_reembed chunks=%d", reembedded)
    except Exception:  # noqa: BLE001 — never fail the sweep on the re-embed pass
        logger.exception("knowledge_reembed_failed")
    return synced
