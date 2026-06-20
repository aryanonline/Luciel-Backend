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
from app.connections.secrets import get_secret_store
from app.db.base import TenantMixin
from app.models.audit import ActorType
from app.models.connection import InstanceConnection
from app.models.conversation import EscalationEvent, Session, SessionSummary, Transcript
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.knowledge import KnowledgeChunk, KnowledgeSource
from app.models.lead import Lead


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


async def hard_delete_cascade(session: AsyncSession, admin_id: uuid.UUID) -> dict[str, int]:
    """Run the ordered cascade for the tenant's Luciel. Returns per-class counts.

    Ordered (Architecture §3.6.5): chunks → sources → transcripts → summaries →
    leads → escalation events → connections → sessions → instance row. The
    admin_audit_log is NEVER touched.
    """
    inst = (
        await session.execute(select(Instance).where(Instance.admin_id == admin_id))
    ).scalar_one_or_none()
    if inst is None:
        return {}
    iid = inst.id
    counts: dict[str, int] = {}
    counts["knowledge_chunks"] = await _log_and_delete(
        session, admin_id, iid, KnowledgeChunk, "knowledge_chunk"
    )
    counts["knowledge_sources"] = await _log_and_delete(
        session, admin_id, iid, KnowledgeSource, "knowledge_source"
    )
    counts["transcripts"] = await _log_and_delete(session, admin_id, iid, Transcript, "transcript")
    counts["session_summaries"] = await _log_and_delete(
        session, admin_id, iid, SessionSummary, "session_summary"
    )
    counts["leads"] = await _log_and_delete(session, admin_id, iid, Lead, "lead_record")
    counts["escalation_events"] = await _log_and_delete(
        session, admin_id, iid, EscalationEvent, "escalation_event"
    )
    # §3.6.5 step 6: delete connection secrets BEFORE removing the rows (§3.8.3 / §3.6.3).
    # Idempotent/best-effort: a missing ref is not an error (LocalSecretStore.delete → False).
    _store = get_secret_store()
    conn_rows = (
        (
            await session.execute(
                select(InstanceConnection).where(InstanceConnection.admin_id == admin_id)
            )
        )
        .scalars()
        .all()
    )
    for _conn in conn_rows:
        if _conn.secret_ref is not None:
            try:
                _deleted = await _store.delete(_conn.secret_ref)
            except Exception:  # noqa: BLE001
                _deleted = False
            await audit.emit(
                session,
                admin_id=admin_id,
                instance_id=iid,
                actor_type=ActorType.platform,
                event_type="connection_secret_cleanup",
                payload={
                    "connection_id": str(_conn.connection_id),
                    "secret_ref": _conn.secret_ref,
                    "deleted": _deleted,
                    "context": "hard_delete_cascade",
                },
            )
    counts["instance_connections"] = await _log_and_delete(
        session, admin_id, iid, InstanceConnection, "instance_connection"
    )
    counts["sessions"] = await _log_and_delete(session, admin_id, iid, Session, "session")

    # Finally the instance row itself → account becomes empty.
    inst.state = LucielState.luciel_hard_deleted
    await session.flush()
    await session.execute(delete(Instance).where(Instance.admin_id == admin_id))

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
