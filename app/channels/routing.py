"""Inbound routing — resolve a destination identifier to exactly one tenant (§3.1.3).

Each channel resolves via the same principle: a stable, channel-specific
destination identifier maps to one (admin_id, instance_id). Resolution runs AFTER
signature verification. An unresolvable destination is rejected (never routed on a
guess). The lookup is a narrow pre-subject read (like auth email lookup): it
resolves the inbound to its tenant, after which app.admin_id is set to that tenant.

Channel → resolution key:
  widget: embed key (data-key)
  email:  recipient address (instance-slug@admin-slug.luciel-mail.com)
  sms/voice: recipient E.164 phone number
  whatsapp/meta: Meta destination id

The destination→tenant mapping lives in instance_connections.non_secret_config for
provisioned channels (the sms_sender number, the email_sender identity), and in
the instance's embed_key_public_id for the widget. This module resolves via a
SECURITY DEFINER lookup analogous to auth_lookup_account (added in this arc's
migration) so resolution works before a tenant subject exists.
"""

from __future__ import annotations

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
