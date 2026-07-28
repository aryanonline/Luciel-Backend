"""instance_tool_authorizations writes — the broker's gate-1 data (Arch §3.3.3).

The admin's tools pillar is stored as JSONB on the instance (what the dashboard reads
back), but the broker's default-deny authorization gate reads the relational
``instance_tool_authorizations`` table. Nothing wrote that table, so a tool switched on
in the UI was still refused with ``not_authorized`` at dispatch. This module is the
single writer, called from the tools pillar endpoint inside the same transaction as the
JSONB write so the two representations cannot diverge.

Disable is a flag flip, not a delete (Vision §3.7.1: "disabling is reversible — its
connection rows and secrets are retained, not purged"), so re-enabling needs no
re-setup and the row's audit trail survives.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool_auth import InstanceToolAuthorization


class ToolToggle(Protocol):
    """The shape of one entry in the tools pillar payload (``AddonToolIn``)."""

    id: str
    enabled: bool


async def sync_tool_authorizations(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    tools: Iterable[ToolToggle],
) -> None:
    """Upsert one authorization row per tool in the request, mirroring ``enabled``.

    The tools pillar is a full-replace, but a tool ABSENT from the payload is left
    untouched rather than disabled: the wire contract sends the whole checklist, and
    silently revoking authorization for something the caller never mentioned would be
    a default-deny surprise in the wrong direction (a tool the admin still sees as on).
    Only ids the caller actually named change state.
    """
    existing = {
        row.tool_id: row
        for row in (
            await session.execute(
                select(InstanceToolAuthorization).where(
                    InstanceToolAuthorization.admin_id == admin_id,
                    InstanceToolAuthorization.instance_id == instance_id,
                )
            )
        ).scalars()
    }
    for tool in tools:
        row = existing.get(tool.id)
        if row is None:
            session.add(
                InstanceToolAuthorization(
                    admin_id=admin_id,
                    instance_id=instance_id,
                    tool_id=tool.id,
                    enabled=tool.enabled,
                )
            )
        else:
            row.enabled = tool.enabled
    await session.flush()
