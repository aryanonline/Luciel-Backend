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

§3.8.7 rule E (config-snapshot) is split across two layers, and this module owns the
SAFETY half: the gate reads LIVE status straight from the row on every call — it never
caches or snapshots — so a mid-conversation disable/revoke or a health-worker
transition to expired/error takes effect on the VERY NEXT attempt (capability=snapshot,
usability=live). The deferred additive half of rule E (a newly enabled tool /
personality / business-context edit applies to the NEXT conversation, not an in-flight
one) is implemented in the session/orchestrator layer: sessions.resolve_or_open_session
freezes the effective capability set onto ``sessions.config_snapshot`` at open, and the
orchestrator sources enabled tools/channels/context from that snapshot. Those two halves
compose: a tool in the snapshot whose connection is now unhealthy is still blocked HERE
(safety wins), and a tool enabled after open is not yet a capability for this session.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connections import registry
from app.models.connection import InstanceConnection

# The single value that satisfies the connection gate (§3.8.4: only `connected`
# dispatches; revoked rows are not even broker-visible).
CONNECTED = "connected"

# Reverse of registry.DEPENDENT_CHANNELS: channel_id -> the connection_type that backs
# it. Built once at import time from the same table the disconnect cascade already
# uses (§5b), so a channel's live status and its cascade dependency can never diverge.
# A connection_type covering >1 channel (channel_auth -> whatsapp + messenger)
# fans out to each; a channel with no backing connection (widget) is simply absent.
_CHANNEL_CONNECTION_TYPE: dict[str, str] = {
    channel_id: connection_type
    for connection_type, channel_ids in registry.DEPENDENT_CHANNELS.items()
    for channel_id in channel_ids
}


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


# Raw instance_connections.status values that ARE live-user-facing on the channels
# pillar (Schemas §ConnectionStatusWire) but were never surfaced there — only
# 'connected' short-circuits the tool broker's gate 3, yet the owner-facing channel
# card needs the FULL honest state (pending/needs-hosting/etc.), not a collapsed
# connected/not-connected bit. Anything not in this allow-list (e.g. a future raw
# status the registry grows before the wire enum catches up) degrades to the closest
# honest existing wire value rather than raising, so a new provider status can never
# 500 the dashboard's main read.
_WIRE_SAFE_STATUSES = frozenset(
    {
        "unconfigured",
        "connected",
        "error",
        "expired",
        "revoked",
        "dormant",
        "not_connected",
        "pending_carrier_registration",
        "pending_email_routing",
    }
)


def _to_wire_status(raw: str | None) -> str:
    """Map a raw ``instance_connections.status`` to the closed wire enum.

    ``not_operable_hosting_required`` (§3.1.4 BYO-number operability) has no wire
    counterpart yet — schema follow-up, tracked separately — so it degrades to
    ``error`` (never silently "connected", never a KeyError) until the wire enum
    grows the value. Every currently-registered status besides that one already has
    a 1:1 wire entry."""
    if raw is None:
        return "unconfigured"
    if raw in _WIRE_SAFE_STATUSES:
        return raw
    return "error"


async def channel_connection_statuses(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
) -> dict[str, str]:
    """LIVE wire-safe connection status for every channel backed by a connection.

    This is the read-side counterpart to ``registry.DEPENDENT_CHANNELS`` / the
    disconnect cascade: a channel absent from ``_CHANNEL_CONNECTION_TYPE`` (``widget``)
    has no connection to report and is simply absent from the returned mapping — the
    caller leaves its stored ``connectionStatus`` (there isn't one) untouched.

    One query per distinct backing connection_type (email_sender, sms_sender,
    channel_auth, instagram_auth), not per channel, so whatsapp and messenger
    sharing ``channel_auth`` do not double-query.

    Fixes the toggle-reconnect bug (owner report, SMS disable/enable): the channels
    pillar's ``connectionStatus`` used to be written ONLY by the client's own PUT echo
    and by ``update_channels``' full-replace — nothing ever synced it from the actual
    ``instance_connections`` row an attach/OAuth-callback updates, so a freshly BYO'd
    Twilio number never made it into the JSONB the dashboard reads, and the channel
    card fell back to its ``unconfigured`` default and asked the owner to reconnect
    even though the connection (and runtime dispatch, gated live via
    ``connection_status`` above) were never touched."""
    connection_types = sorted(set(_CHANNEL_CONNECTION_TYPE.values()))
    by_type: dict[str, str | None] = {}
    for connection_type in connection_types:
        by_type[connection_type] = await connection_status(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            connection_type=connection_type,
        )
    return {
        channel_id: _to_wire_status(by_type[connection_type])
        for channel_id, connection_type in _CHANNEL_CONNECTION_TYPE.items()
    }
