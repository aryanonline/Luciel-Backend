"""Luciel / account lifecycle state machine (Architecture §3.6).

Owner-only transitions. The two nested lifecycles:
  Delete Luciel ⊆ Close account.

Luciel:  active ⇄ paused ; (active|paused) --delete--> luciel_grace_window
         luciel_grace_window --restore--> active (NOT paused)
         luciel_grace_window --day30--> luciel_hard_deleted (cascade) -> account: empty
Account: empty --close (export-first)--> account_closing --grace--> account_hard_deleted
         (close requires the Luciel already deleted)

Each transition emits ``lifecycle_transition`` to admin_audit_log. The delete
transition performs the fused quiesce (stop intake → drain → cascade-stage) as
one atomic owner action — no manual pause-first.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.models.audit import ActorType
from app.models.connection import InstanceConnection
from app.models.instance import Instance, LucielState
from app.models.knowledge import KnowledgeSource


def _now() -> datetime:
    return datetime.now(UTC)


class LifecycleError(Exception):
    """Illegal transition (mapped to 409 conflict at the API edge)."""


async def _get_instance(session: AsyncSession, admin_id: uuid.UUID) -> Instance:
    inst = (
        await session.execute(select(Instance).where(Instance.admin_id == admin_id))
    ).scalar_one_or_none()
    if inst is None:
        raise LifecycleError("no_luciel")
    return inst


async def _emit_transition(
    session: AsyncSession,
    inst: Instance,
    *,
    actor_user_id: uuid.UUID | None,
    frm: str,
    to: str,
) -> None:
    await audit.emit(
        session,
        admin_id=inst.admin_id,
        instance_id=inst.id,
        actor_user_id=actor_user_id,
        actor_type=ActorType.admin_user if actor_user_id else ActorType.platform,
        event_type="lifecycle_transition",
        payload={"from": frm, "to": to},
    )


async def pause(session: AsyncSession, admin_id: uuid.UUID, actor_user_id: uuid.UUID) -> Instance:
    inst = await _get_instance(session, admin_id)
    if inst.state != LucielState.active:
        raise LifecycleError("can_only_pause_active")
    frm = inst.state.value
    inst.state = LucielState.paused
    await session.flush()
    await _emit_transition(session, inst, actor_user_id=actor_user_id, frm=frm, to="paused")
    return inst


async def resume(session: AsyncSession, admin_id: uuid.UUID, actor_user_id: uuid.UUID) -> Instance:
    inst = await _get_instance(session, admin_id)
    if inst.state != LucielState.paused:
        raise LifecycleError("can_only_resume_paused")
    inst.state = LucielState.active
    await session.flush()
    await _emit_transition(session, inst, actor_user_id=actor_user_id, frm="paused", to="active")
    return inst


async def delete_luciel(
    session: AsyncSession, admin_id: uuid.UUID, actor_user_id: uuid.UUID
) -> Instance:
    """Fused-quiesce delete → luciel_grace_window (Architecture §3.6.3).

    Quiesce: revoke embed key (intake stops; webhooks 200-no-op handled at the
    edge), then stage the cascade — connections → revoked + secret cleanup
    enqueued; knowledge soft-deleted; state → grace_window with stamp.
    Deletable from active OR paused (no manual pause-first).
    """
    inst = await _get_instance(session, admin_id)
    if inst.state not in (LucielState.active, LucielState.paused):
        raise LifecycleError("cannot_delete_from_state")
    frm = inst.state.value

    # 0. Quiesce: revoke embed key so no new inbound is accepted. The key VALUE
    # is stashed, not destroyed: restore-within-grace must revive the SAME key
    # ("exactly as it was", §3.6.4) or the customer's deployed embed snippet is
    # permanently dead after a restore. The day-30 cascade deletes the whole
    # instance row, stash included.
    inst.embed_key_stashed = inst.embed_key_public_id
    inst.embed_key_public_id = None

    # 1. instance_connections → revoked; secret destruction is SCHEDULED for the
    # day-30 cascade (restore must be able to revive the connection). The audit
    # event says exactly that — `connection_secret_cleanup` is reserved for the
    # moment a secret is actually destroyed (cascade.py / service.disconnect).
    conns = (
        (
            await session.execute(
                select(InstanceConnection).where(InstanceConnection.admin_id == admin_id)
            )
        )
        .scalars()
        .all()
    )
    for c in conns:
        if c.status != "revoked":
            c.status = "revoked"
            await audit.emit(
                session,
                admin_id=admin_id,
                instance_id=inst.id,
                event_type="connection_secret_cleanup_scheduled",
                actor_type=ActorType.platform,
                payload={
                    "connection_id": str(c.connection_id),
                    "secret_ref_present": bool(c.secret_ref),
                    "scheduled_for": "grace_expiry_hard_delete_cascade",
                },
            )

    # 2. knowledge_sources → soft_deleted.
    sources = (
        (await session.execute(select(KnowledgeSource).where(KnowledgeSource.admin_id == admin_id)))
        .scalars()
        .all()
    )
    for src in sources:
        src.soft_deleted_at = _now()

    # 3. state → grace window.
    inst.state = LucielState.luciel_grace_window
    inst.grace_window_started_at = _now()
    inst.soft_deleted_at = _now()
    await session.flush()
    await _emit_transition(
        session, inst, actor_user_id=actor_user_id, frm=frm, to="luciel_grace_window"
    )
    return inst


async def restore(session: AsyncSession, admin_id: uuid.UUID, actor_user_id: uuid.UUID) -> Instance:
    """Restore within grace → ACTIVE (not paused) (Architecture §3.6.4)."""
    inst = await _get_instance(session, admin_id)
    if inst.state != LucielState.luciel_grace_window:
        raise LifecycleError("not_in_grace_window")
    inst.state = LucielState.active
    inst.grace_window_started_at = None
    inst.soft_deleted_at = None
    # Revive the SAME embed key the delete stashed ("exactly as it was",
    # §3.6.4): the snippet already deployed on the customer's site starts
    # working again with no re-edit. Minting fresh is only the fallback for a
    # legacy row that entered grace before the stash column existed.
    inst.embed_key_public_id = inst.embed_key_stashed or f"vm_live_{uuid.uuid4().hex[:12]}"
    inst.embed_key_stashed = None
    conns = (
        (
            await session.execute(
                select(InstanceConnection).where(InstanceConnection.admin_id == admin_id)
            )
        )
        .scalars()
        .all()
    )
    # c21 hardening — restore is the ONE legitimate un-revoke, and un-revoking a row
    # re-arms its destination claim in the inbound resolver. Two guards:
    #   1. Another tenant may have LEGITIMATELY claimed the destination during the
    #      30-day grace (the number/address was released the moment this tenant's
    #      rows went revoked). Restoring the claim would capture their inbound —
    #      so a foreign-held destination is STRIPPED and the row comes back
    #      unconfigured with an honest detail, never silently re-routed.
    #   2. If this tenant re-attached the same connection TYPE during grace (a new
    #      non-revoked row exists), un-revoking the old row would violate the
    #      single-active-per-type unique index — the superseded row stays revoked.
    from app.channels import routing  # noqa: PLC0415 — circular at module import

    active_types = {c.connection_type for c in conns if c.status != "revoked"}
    for c in conns:
        if c.status != "revoked":
            continue
        if c.connection_type in active_types:
            continue  # superseded during grace; the newer row is the live one
        cfg = dict(c.non_secret_config or {})
        destinations = [d for d in [cfg.get("destination")] if isinstance(d, str) and d]
        dest_map = cfg.get("destinations")
        if isinstance(dest_map, dict):
            destinations += [v for v in dest_map.values() if isinstance(v, str) and v]
        lost = False
        for dest in destinations:
            holder = await routing.owner_of_destination(session, c.connection_type, dest)
            if holder is not None and holder != admin_id:
                lost = True
                break
        if lost:
            for key in ("destination", "destinations", "address", "mode"):
                cfg.pop(key, None)
            c.non_secret_config = cfg
            c.status_detail = (
                "While this Luciel was deleted, this number or address was connected "
                "to a different account — designate a new one to bring the channel back."
            )
        # Pre-delete connections were revoked; restore to 'unconfigured' so the
        # health worker / reconnect path re-establishes them (never silently 'connected').
        c.status = "unconfigured"
    sources = (
        (await session.execute(select(KnowledgeSource).where(KnowledgeSource.admin_id == admin_id)))
        .scalars()
        .all()
    )
    for src in sources:
        src.soft_deleted_at = None
    await session.flush()
    await _emit_transition(
        session, inst, actor_user_id=actor_user_id, frm="luciel_grace_window", to="active"
    )
    return inst
