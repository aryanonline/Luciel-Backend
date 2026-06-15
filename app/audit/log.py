"""Append-only, hash-chained audit writer (Architecture §5.2 / §5.3).

The chain is per-admin_id: each new row's ``prev_hash`` is the SHA-256 of the
prior row's ``event_id || created_at || event_payload`` (canonical JSON). The
genesis row for a tenant has ``prev_hash = NULL``.

Because the table is INSERT-only at the grant level, the writer never updates;
it computes the chain link from the latest existing row for the tenant and
inserts. PII rule: callers must pass only hashed resolved_lead_id and
non-identifying metadata in ``payload`` (Architecture §3.4.11).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.events import EVENT_TYPES
from app.models.audit import ActorType, AdminAuditLog


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(event_id: uuid.UUID, created_at: datetime, payload: dict[str, object]) -> str:
    material = f"{event_id}{created_at.isoformat()}{_canonical(payload)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def emit(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    event_type: str,
    actor_type: ActorType,
    payload: dict[str, object] | None = None,
    instance_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> AdminAuditLog:
    """Append one audit row, chaining from the tenant's latest row.

    Runs inside the caller's transaction/session (which already has app.admin_id
    set for the tenant), so RLS scopes the "latest row" lookup to this tenant.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown audit event_type: {event_type!r}")
    payload = payload or {}

    # Find the tenant's current chain head, ordered by the monotonic seq (created_at
    # is transaction-constant, so it cannot order rows written in one txn). RLS
    # guarantees this is tenant-scoped. flush() makes prior same-txn rows visible.
    await session.flush()
    latest = (
        await session.execute(
            select(AdminAuditLog)
            .where(AdminAuditLog.admin_id == admin_id)
            .order_by(AdminAuditLog.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    prev_hash: str | None = None
    if latest is not None:
        prev_hash = compute_hash(latest.event_id, latest.created_at, latest.event_payload)

    row = AdminAuditLog(
        admin_id=admin_id,
        instance_id=instance_id,
        actor_user_id=actor_user_id,
        actor_type=actor_type,
        event_type=event_type,
        event_payload=payload,
        prev_hash=prev_hash,
    )
    session.add(row)
    await session.flush()
    return row


async def verify_chain(session: AsyncSession, admin_id: uuid.UUID) -> bool:
    """Replay a tenant's chain and confirm integrity (used by tests + tooling)."""
    rows = (
        (
            await session.execute(
                select(AdminAuditLog)
                .where(AdminAuditLog.admin_id == admin_id)
                .order_by(AdminAuditLog.seq.asc())
            )
        )
        .scalars()
        .all()
    )
    expected_prev: str | None = None
    for row in rows:
        if row.prev_hash != expected_prev:
            return False
        expected_prev = compute_hash(row.event_id, row.created_at, row.event_payload)
    return True
