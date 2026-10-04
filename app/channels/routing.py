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


def normalize_email_destination(value: str) -> str:
    """An email destination's canonical spelling (round 7 WP-3c item 10, owner ruling
    2026-09-14: email claims are case-insensitive). Every producer stores this form,
    migration 0067 rewrote the rows that predate it, and the 0067 partial unique
    index is on ``lower(destination)`` — so the byte-exact resolver only ever meets
    one spelling, provided the value being looked up is spelled the same way."""
    return value.strip().lower()


async def _resolve(
    session: AsyncSession, connection_type: str, destination: str, *, include_quiescing: bool
) -> InstanceContext:
    row = (
        await session.execute(
            text(
                "SELECT admin_id, instance_id "
                "FROM channel_resolve_destination(:ct, :dest, :quiescing)"
            ),
            {"ct": connection_type, "dest": destination, "quiescing": include_quiescing},
        )
    ).one_or_none()
    if row is None:
        raise InboundRejected(f"unresolvable_destination:{connection_type}")
    return InstanceContext(admin_id=row.admin_id, instance_id=row.instance_id)


async def resolve_destination(
    session: AsyncSession, connection_type: str, destination: str
) -> InstanceContext:
    """Resolve a channel destination (phone number / email address / Meta id) to a tenant.

    Matches against instance_connections.non_secret_config->>'destination' for the
    given connection_type, over non-revoked rows, via a SECURITY DEFINER function.
    A ``quiescing`` row (a Luciel in its delete drain, round 7 WP-5) IS admitted here:
    the customer mid-conversation at the delete instant must keep reaching the tenant
    so the §3.6.3 drain can finish on their channel. An email destination is
    case-folded first (round 7 WP-3c item 10).
    """
    if connection_type == "email_sender":
        destination = normalize_email_destination(destination)
    return await _resolve(session, connection_type, destination, include_quiescing=True)


async def owner_of_destination(
    session: AsyncSession, connection_type: str, destination: str
) -> uuid.UUID | None:
    """Which tenant currently holds this destination, if any (None = unclaimed).

    Used when an admin CLAIMS a destination, to reject one another tenant already holds.
    It goes through the same SECURITY DEFINER resolver as inbound routing on purpose: RLS
    hides other tenants' rows from an ordinary query, so a plain SELECT would report every
    foreign claim as unclaimed. Sharing the resolver also means the answer cannot disagree
    with the row ``resolve_destination`` will actually pick at delivery time — with ONE
    deliberate difference: a ``quiescing`` row routes inbound (the drain) but holds no
    claim, so a deleted Luciel releases its number/address for the grace window exactly
    as a revoked one did (c21), and the newest live claim wins from that moment on.

    An email claim is case-folded BEFORE the holder check (round 7 WP-3c item 10):
    ``Owner@Fabrikam.Example`` and ``owner@fabrikam.example`` are one mailbox, and a
    claim that differed only in case used to slip past the gate.
    """
    if connection_type == "email_sender":
        destination = normalize_email_destination(destination)
    try:
        return (
            await _resolve(session, connection_type, destination, include_quiescing=False)
        ).admin_id
    except InboundRejected:
        return None


#: Inbound Meta resolution tries BOTH grants for an entry id (meta_messaging_adapter),
#: so a claim on either must be visible to a claim on the other (audit F052).
META_GRANT_CONNECTION_TYPES: tuple[str, ...] = ("instagram_auth", "channel_auth")


async def owner_of_meta_destination(session: AsyncSession, destination: str) -> uuid.UUID | None:
    """Which tenant holds this Meta asset id on EITHER Meta grant, if any.

    A page/IG id bound under ``instagram_auth`` by tenant A used to be invisible to
    tenant B's ``channel_auth`` bind (and vice versa), while inbound delivery resolved
    across both in a fixed order — so the same id could be claimed twice and one
    tenant's messages arrived at the other's Luciel.
    """
    for connection_type in META_GRANT_CONNECTION_TYPES:
        holder = await owner_of_destination(session, connection_type, destination)
        if holder is not None:
            return holder
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


async def holders_of_connection_config(
    session: AsyncSession, connection_type: str, key: str, value: str
) -> list[uuid.UUID]:
    """Every tenant whose non-revoked ``connection_type`` row carries ``key = value``
    in its non-secret config (round 7 WP-3c item 13).

    A shared CRM portal (the same HubSpot ``hubId`` / Salesforce ``orgId`` connected
    by two accounts) is ALLOWED — owner ruling 2026-09-14 — with an informational
    warning, and RLS hides the other account's row from an ordinary query, so this
    goes through the narrow SECURITY DEFINER lookup migration 0067 added. It reads
    tenant ids only; the caller decides what (if anything) to tell the owner.
    """
    if not value:
        return []
    rows = (
        await session.execute(
            text("SELECT admin_id FROM connection_config_holders(:ct, :key, :value)"),
            {"ct": connection_type, "key": key, "value": value},
        )
    ).all()
    return [row.admin_id for row in rows]
