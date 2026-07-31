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


def hash_lead_id(lead_id: uuid.UUID | str) -> str:
    """Stable, non-reversible digest of a lead identifier for audit payloads.

    Architecture §3.4.11 / §5.3: audit events that reference a lead must store the
    HASHED ``resolved_lead_id`` (and non-identifying metadata) in the immutable,
    hash-chained payload — never the raw id — so PIPEDA lead erasure is reconciled
    with audit immutability by data placement + key destruction, not by rewriting
    history. After the lead row (and its display-lookup) are erased, an audit row
    still proves *that* an event happened and *when*, but resolves to no recoverable
    identity. This is a one-way SHA-256 digest; the raw UUID never enters the chain.
    """
    return hashlib.sha256(str(lead_id).encode("utf-8")).hexdigest()


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
    # Anchored on the OLDEST SURVIVING row rather than on NULL. Once the 1-year
    # retention has pruned a tenant's aged prefix (§B5, P1-15), that row still carries
    # the hash of a row that no longer exists, and demanding a NULL head here would
    # report every long-lived tenant as tampered with. Verifying linkage from the
    # anchor still catches what immutability is about — a row removed or rewritten
    # inside the surviving span — while accepting the one truncation §B5 authorizes.
    expected_prev: str | None = rows[0].prev_hash if rows else None
    for row in rows:
        if row.prev_hash != expected_prev:
            return False
        expected_prev = compute_hash(row.event_id, row.created_at, row.event_payload)
    return True
