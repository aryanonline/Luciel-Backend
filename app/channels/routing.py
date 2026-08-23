"""Inbound routing — resolve a destination identifier to exactly one tenant (§3.1.3).

Each channel resolves via the same principle: a stable, channel-specific
destination identifier maps to one (admin_id, instance_id). Resolution runs AFTER
signature verification. An unresolvable destination is rejected (never routed on a
guess). The lookup is a narrow pre-subject read (like auth email lookup): it
resolves the inbound to its tenant, after which app.admin_id is set to that tenant.

Channel → resolution key:
  widget: embed key (data-key)
  email:  recipient address — the tenant's provisioned email address (own-domain or
          VM-subdomain), stored on the email_sender connection (§3.1.6a)
  sms/voice: recipient E.164 phone number
  whatsapp/meta: Meta destination id

The destination→tenant mapping lives in instance_connections.non_secret_config for
the configured channels (the tenant's BYO sms_sender number, the email_sender
identity), and in the instance's embed_key_public_id for the widget. This module
resolves via a
SECURITY DEFINER lookup analogous to auth_lookup_account (added in this arc's
migration) so resolution works before a tenant subject exists.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import InboundRejected, InstanceContext


async def resolve_widget(session: AsyncSession, embed_key: str) -> InstanceContext:
    row = (
        await session.execute(
            text("SELECT admin_id, instance_id FROM channel_resolve_embed_key(:k)"),
            {"k": embed_key},
        )
    ).one_or_none()
    if row is None:
        raise InboundRejected("unresolvable_embed_key")
    return InstanceContext(admin_id=row.admin_id, instance_id=row.instance_id)


async def resolve_destination(
    session: AsyncSession, connection_type: str, destination: str
) -> InstanceContext:
    """Resolve a channel destination (phone number / email address / Meta id) to a tenant.

    Matches against instance_connections.non_secret_config->>'destination' for the
    given connection_type, over non-revoked rows, via a SECURITY DEFINER function.
    """
    row = (
        await session.execute(
            text("SELECT admin_id, instance_id FROM channel_resolve_destination(:ct, :dest)"),
            {"ct": connection_type, "dest": destination},
        )
    ).one_or_none()
    if row is None:
        raise InboundRejected(f"unresolvable_destination:{connection_type}")
    return InstanceContext(admin_id=row.admin_id, instance_id=row.instance_id)


async def owner_of_destination(
    session: AsyncSession, connection_type: str, destination: str
) -> uuid.UUID | None:
    """Which tenant currently holds this destination, if any (None = unclaimed).

    Used when an admin CLAIMS a destination, to reject one another tenant already holds.
    It goes through the same SECURITY DEFINER resolver as inbound routing on purpose: RLS
    hides other tenants' rows from an ordinary query, so a plain SELECT would report every
    foreign claim as unclaimed. Sharing the resolver also means the answer cannot disagree
    with the row ``resolve_destination`` will actually pick at delivery time.
    """
    try:
        return (await resolve_destination(session, connection_type, destination)).admin_id
    except InboundRejected:
        return None


async def lock_destination_claim(session: AsyncSession, destination: str) -> None:
    """Serialize destination CLAIMS on this destination for the current transaction.

    The claim gates are check-then-write: two concurrent claims for the same
    destination in different tenants could both see ``owner_of_destination() is
    None`` and both bind (TOCTOU). A transaction-scoped advisory lock around
    check+write closes it: the loser BLOCKS (unlike the sweeps' ``try`` variant —
    here the loser has something to wait for: the winner's commit, after which the
    re-check answers the honest 409). Keys are two int4s derived in Python (the
    sweeps' idiom, db/session.py: ``hashtext`` is not stable across PG versions).
    Postgres releases the lock on commit OR rollback, so a failed claim can never
    wedge the destination.
    """
    import zlib  # noqa: PLC0415

    k1 = zlib.crc32(b"dest_claim") - 2**31
    k2 = zlib.crc32(destination.strip().lower().encode()) - 2**31
    await session.execute(text("SELECT pg_advisory_xact_lock(:k1, :k2)"), {"k1": k1, "k2": k2})
