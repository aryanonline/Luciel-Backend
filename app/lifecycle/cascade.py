"""Hard-delete cascade + account closure (Architecture §3.6.5 / §3.6.6).

The Luciel hard-delete cascade fires at day 30 of the grace window (or on an
explicit skip-grace confirm). Ordered to respect FK dependencies and to LOG each
data class deletion (``data_retention_hard_delete``) BEFORE the rows are gone.
It destroys the Luciel and everything attached, but NOT the account shell and NOT
the admin_audit_log (the proof of deletion is retained, §3.6.5).
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.connections.secrets import get_secret_store, tenant_secret_prefix
from app.db.base import TenantMixin
from app.models.audit import ActorType
from app.models.billing import BillingAccount, ProcessedStripeEvent
from app.models.budget import ConversationBudget, CountedSession
from app.models.connection import InstanceConnection
from app.models.conversation import EscalationEvent, Session, SessionSummary, Transcript
from app.models.deferred import DeferredAction
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.knowledge import KnowledgeChunk, KnowledgeEdge, KnowledgeEntity, KnowledgeSource
from app.models.lead import Lead
from app.models.tool_auth import InstanceToolAuthorization
from app.models.voice import VoiceCallRecord

#: Tables that carry an admin_id but must NOT be cascaded, each for its own reason:
#:   accounts             — the shell outlives its Luciel (§3.6.5); closure destroys it.
#:   admin_audit_log      — the proof the deletion happened. Erasing it erases the evidence.
#:   internal_access_log  — a platform-STAFF access record (§5.11) with a 3-year retention
#:                          floor, not tenant-owned data. Its admin_id names which tenant
#:                          staff looked at, so a tenant deleting themselves must not be
#:                          able to erase the record of that access.
CASCADE_EXEMPT_TABLES = frozenset({"accounts", "admin_audit_log", "internal_access_log"})

#: Every tenant-scoped table the cascade destroys, child data first. The schema declares
#: no foreign keys at all, so nothing in Postgres will cascade on our behalf and nothing
#: will error if a table is forgotten — a missed table just leaves a live tenant's rows
#: behind under a deleted admin_id. ``tests/integration/test_deletion_cascade_completeness``
#: walks information_schema against this tuple; that test is the only backstop there is.
CASCADE_MODELS: tuple[tuple[type[TenantMixin], str], ...] = (
    (KnowledgeChunk, "knowledge_chunk"),
    (KnowledgeEdge, "knowledge_edge"),
    (KnowledgeEntity, "knowledge_entity"),
    (KnowledgeSource, "knowledge_source"),
    (Transcript, "transcript"),
    (SessionSummary, "session_summary"),
    (Lead, "lead_record"),
    (EscalationEvent, "escalation_event"),
    (VoiceCallRecord, "voice_call_record"),
    (DeferredAction, "deferred_action"),
    (InstanceToolAuthorization, "instance_tool_authorization"),
    (ConversationBudget, "conversation_budget"),
    (CountedSession, "counted_session"),
    (ProcessedStripeEvent, "processed_stripe_event"),
    (BillingAccount, "billing_account"),
    (InstanceConnection, "instance_connection"),
    (Session, "session"),
    (Instance, "luciel_instance"),
)


async def _log_and_delete(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    model: type[TenantMixin],
    data_class: str,
) -> int:
    """Log a data_retention_hard_delete for the class, then delete its rows."""
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        event_type="data_retention_hard_delete",
        actor_type=ActorType.platform,
        payload={
            "data_class": data_class,
            "retention_policy_applied": "luciel_hard_delete_cascade",
        },
    )
    result = await session.execute(delete(model).where(model.admin_id == admin_id))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def _delete_tenant_secrets(
    session: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID
) -> None:
    """Destroy the tenant's credential material before the rows that reference it.

    Two passes, because neither alone is sufficient. Per-``secret_ref`` deletion produces
    the auditable per-connection record §3.8.3 rule 4 wants, but only reaches secrets a
    surviving row still names. The ``vm/{admin_id}/`` prefix sweep catches the rest: a
    connection deleted earlier, or re-keyed to another type, leaves live credentials in
    the backend that Postgres can no longer name.

    Best-effort by design: a store that is momentarily unreachable must not abort a
    deletion the owner already confirmed. The failure is recorded, not swallowed.
    """
    store = get_secret_store()
    conn_rows = (
        (
            await session.execute(
                select(InstanceConnection).where(InstanceConnection.admin_id == admin_id)
            )
        )
        .scalars()
        .all()
    )
    for conn in conn_rows:
        if conn.secret_ref is None:
            continue
        try:
            deleted = await store.delete(conn.secret_ref)
        except Exception:  # noqa: BLE001 — a broken store must not strand the deletion
            deleted = False
        await audit.emit(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            actor_type=ActorType.platform,
            event_type="connection_secret_cleanup",
            payload={
                "connection_id": str(conn.connection_id),
                "secret_ref": conn.secret_ref,
                "deleted": deleted,
                "context": "hard_delete_cascade",
            },
        )

    prefix = tenant_secret_prefix(admin_id)
    try:
        swept = await store.delete_prefix(prefix)
        error: str | None = None
    except Exception as exc:  # noqa: BLE001
        swept, error = 0, type(exc).__name__
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        actor_type=ActorType.platform,
        event_type="connection_secret_cleanup",
        payload={
            "secret_ref_prefix": prefix,
            "deleted_count": swept,
            "error": error,
            "context": "hard_delete_cascade_prefix_sweep",
        },
    )


async def hard_delete_cascade(session: AsyncSession, admin_id: uuid.UUID) -> dict[str, int]:
    """Run the ordered cascade for the tenant's Luciel. Returns per-table row counts.

    Destroys every table in ``CASCADE_MODELS`` (child data first, instance row last) plus
    the tenant's secret material. The account shell and the admin_audit_log survive
    (§3.6.5).
    """
    inst = (
        await session.execute(select(Instance).where(Instance.admin_id == admin_id))
    ).scalar_one_or_none()
    if inst is None:
        return {}
    iid = inst.id
    await _delete_tenant_secrets(session, admin_id, iid)

    inst.state = LucielState.luciel_hard_deleted
    await session.flush()
    counts = {
        model.__tablename__: await _log_and_delete(session, admin_id, iid, model, data_class)
        for model, data_class in CASCADE_MODELS
    }

    # Mark account empty: hasLuciel false, account shell survives.
    acct = (
        await session.execute(select(Account).where(Account.id == admin_id))
    ).scalar_one_or_none()
    if acct is not None:
        acct.has_completed_first_run = acct.has_completed_first_run  # unchanged; shell survives
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=iid,
        event_type="lifecycle_transition",
        actor_type=ActorType.platform,
        payload={"from": "luciel_hard_deleted", "to": "account:empty"},
    )
    return counts


async def close_account(
    session: AsyncSession, admin_id: uuid.UUID, actor_user_id: uuid.UUID
) -> None:
    """Close the account shell (Architecture §3.6.6). Precondition: Luciel deleted.

    If a Luciel still exists, the close flow runs delete + cascade first (the
    nested model), then destroys the shell. Audit tombstones are retained.
    """
    inst = (
        await session.execute(select(Instance).where(Instance.admin_id == admin_id))
    ).scalar_one_or_none()
    if inst is not None:
        # Subsumes deletion: cascade the Luciel first (export is offered upstream).
        await hard_delete_cascade(session, admin_id)

    await audit.emit(
        session,
        admin_id=admin_id,
        event_type="account_closure_initiated",
        actor_type=ActorType.admin_user,
        actor_user_id=actor_user_id,
    )
    acct = (
        await session.execute(select(Account).where(Account.id == admin_id))
    ).scalar_one_or_none()
    if acct is not None:
        acct.state = AccountState.closed
    await session.flush()
    await audit.emit(
        session,
        admin_id=admin_id,
        event_type="lifecycle_transition",
        actor_type=ActorType.admin_user,
        actor_user_id=actor_user_id,
        payload={"from": "account:empty", "to": "account_closing"},
    )
