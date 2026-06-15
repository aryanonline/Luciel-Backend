"""The universal gate (Architecture §3.8.7 rule A) — one source of truth.

§3.8.7 rule A: a dependency is usable on a given attempt only if BOTH hold:
  (1) it is enabled/authorized (the configuration gate — owner switched it on), and
  (2) its connection is healthy (``status == 'connected'`` in instance_connections,
      §3.8.4 — the connection gate).

This module owns gate (2), the connection-health half, for EVERY dependency type
(tools, channels, knowledge sources, escalation delivery) — "a connection is a
connection" (§3.8.5). The Arc 12 tool broker's inline gate-2 is refactored to call
``connection_status`` / ``connection_healthy`` here so there is exactly one place
that decides what "connected" means (§3.8.7 rule A consolidation).

§3.8.7 rule E (config-snapshot, safety side): the gate reads LIVE status straight
from the row on every call — it never caches or snapshots — so a mid-conversation
disable/revoke or a health-worker transition to expired/error takes effect on the
VERY NEXT attempt. This is the safety-critical immediate-effect behavior; the
deferred additive-snapshot side of rule E (new tools/personality apply to the next
conversation) is NOT implemented here and is flagged in the PR.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import InstanceConnection

# The single value that satisfies the connection gate (§3.8.4: only `connected`
# dispatches; revoked rows are not even broker-visible).
CONNECTED = "connected"


async def connection_status(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    connection_type: str,
) -> str | None:
    """LIVE status of the (non-revoked) connection of this type, or None if absent.

    Tenant-scoped (admin_id + instance_id); RLS is the backstop. Revoked rows are
    excluded so the broker only ever sees a live connection (§3.8.4: revoked rows
    are retained but "not queryable by broker"). Reading live on every call is what
    makes the mid-conversation revoke immediate (§3.8.7 rule E safety side)."""
    return (
        await session.execute(
            select(InstanceConnection.status).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.instance_id == instance_id,
                InstanceConnection.connection_type == connection_type,
                InstanceConnection.status != "revoked",
            )
        )
    ).scalar_one_or_none()


async def connection_healthy(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    connection_type: str,
) -> bool:
    """Gate (2): True iff a live connection of this type is ``connected`` (§3.8.7 A)."""
    return (
        await connection_status(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            connection_type=connection_type,
        )
        == CONNECTED
    )


def gate_failure_reason(connection_type: str, status: str | None) -> str:
    """The uniform structured reason string for a failed connection gate.

    ``connection_not_ready:{type}:{status}`` — a missing connection reads as
    ``unconfigured`` (§3.8.4). This is the one shape every caller (broker, channel
    bind, escalation delivery) surfaces, so the agentic loop reasons about a
    connection failure exactly like any other structured tool outcome (§3.3.3 /
    §3.4.1 / §3.8.7 rule A)."""
    return f"connection_not_ready:{connection_type}:{status or 'unconfigured'}"
