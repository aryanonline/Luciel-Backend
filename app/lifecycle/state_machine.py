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

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.billing import dunning
from app.connections.service import clear_sync_job_on_removal
from app.models.audit import ActorType
from app.models.billing import BillingAccount
from app.models.connection import NOT_SERVING_STATUSES, QUIESCING, InstanceConnection
from app.models.crm_link import CrmLeadLink
from app.models.instance import Instance, LucielState
from app.models.knowledge import KnowledgeChunk, KnowledgeEdge, KnowledgeEntity, KnowledgeSource

__all__ = [
    "NOT_SERVING_STATUSES",
    "QUIESCING",
    "LifecycleError",
    "delete_luciel",
    "pause",
    "restore",
    "resume",
    "serving_status",
]


def _now() -> datetime:
    return datetime.now(UTC)


class LifecycleError(Exception):
    """Illegal transition (mapped to 409 conflict at the API edge)."""


# ``QUIESCING`` — the connection status a Luciel delete writes (round 7 WP-5, item 1;
# the constant lives on ``app.models.connection`` so the gate, the outbound seams and
# the mailbox poll import it without a cycle). A row in this state is DRAINING: the
# inbound resolver still routes its destination to this tenant
# (``channel_resolve_destination``, migration 0068, admits ``quiescing`` rows for
# inbound routing) so a customer mid-conversation at the delete instant keeps getting
# answers on their channel, and the outbound seams may still send on it — but only
# for the in-flight session the runtime drain gate admits; every NEW conversation is
# refused with the honest drain reply. Before this status existed the delete wrote
# ``revoked``, the resolver dropped every non-widget inbound as
# ``unresolvable_destination``, and the "drain" was silence forever for anyone who
# was mid-text.
#
# A quiescing row does NOT hold its destination claim (c21): the claim gate asks the
# resolver with ``p_include_quiescing = false`` and the 0045 uniqueness index excludes
# quiescing rows, so another tenant may legitimately claim the number/address during
# the grace window exactly as before — and restore strips a foreign-held destination.
# The resolver's newest-claim-wins ordering routes inbound to the live claimant from
# that moment on. Nor is it broker-visible (``gate.connection_status`` excludes it
# exactly as it excludes revoked), health-swept (not a health-owned status) or counted
# by the single-active-per-type index (0068 recreates ``uq_conn_active_type``).
#
# ``NOT_SERVING_STATUSES`` — what restore revives and the day-30 cascade destroys:
# both a quiescing row (what delete now writes) and a revoked one (a pre-WP-5 delete).


def serving_status(conn: InstanceConnection) -> str:
    """The status the OUTBOUND seams should judge operability by.

    A quiescing row is a live row wearing a drain marker: the credential is intact
    (secret destruction is scheduled for the day-30 cascade) and the pre-delete status
    sits in ``status_stashed``. The senders gate on ``connected`` / the SMS operable
    set, so without this a quiescing row refused the very drain reply the delete owes
    the customer. Everything else answers with its own status; a quiescing row with
    no stash (never possible from ``delete_luciel``, defensive only) reads as
    ``unconfigured`` — never as connected."""
    if conn.status == QUIESCING:
        return conn.status_stashed or "unconfigured"
    return conn.status


async def _get_instance(
    session: AsyncSession, admin_id: uuid.UUID, *, for_update: bool = False
) -> Instance:
    stmt = select(Instance).where(Instance.admin_id == admin_id)
    if for_update:
        stmt = stmt.with_for_update()
    inst = (await session.execute(stmt)).scalar_one_or_none()
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

    # 0. Quiesce (C8): the embed key STAYS live through the grace window — the
    # resolver admits grace-window instances (migration 0048) precisely so the
    # channel gates can serve the §3.6.3 drain: a conversation in flight at the
    # delete instant finishes on its channel, while every gate refuses NEW
    # sessions for a non-serving state (find_open_session, never open). A stashed
    # copy is still written so restore's same-key revival and pre-0048 grace rows
    # keep one code path; the day-30 cascade deletes the whole instance row, key
    # and stash included.
    inst.embed_key_stashed = inst.embed_key_public_id

    # 1. instance_connections → QUIESCING (round 7 WP-5, item 1); secret destruction
    # is SCHEDULED for the day-30 cascade (restore must be able to revive the
    # connection). The audit event says exactly that — `connection_secret_cleanup`
    # is reserved for the moment a secret is actually destroyed (cascade.py /
    # service.disconnect). Quiescing, not revoked: the resolver (migration 0068)
    # keeps routing the tenant's number / address / Meta id to this instance, so
    # the §3.6.3 drain the runtime gate serves is reachable on every channel, not
    # only the widget. The day-30 cascade and restore treat quiescing exactly as
    # revoked (NOT_SERVING_STATUSES).
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
        if c.status not in NOT_SERVING_STATUSES:
            # Stash the pre-delete status (C8, migration 0049): §3.6.4 promises a
            # restored Luciel comes back "exactly as it was", which includes each
            # connection's honest chip — a healthy calendar restores Connected, an
            # action-needed number restores its action-needed truth. The stash is
            # what makes that possible; blanket 'unconfigured' told every restored
            # owner to reconnect everything. The outbound seams read the stash
            # through ``serving_status`` while the row drains.
            #
            # A billing-DORMANT action tool already carries a stash: the status it
            # held before dunning put it to sleep (``dunning.set_dormant_for_action_tools``).
            # Overwriting that with "dormant" lost the credential's real status, so a
            # close → reopen → payment brought a healthy calendar/CRM back
            # 'unconfigured' (round 7 money review, finding 2). The pre-dormant stash
            # is kept; dormancy is a billing fact, and ``restore`` re-derives it from
            # the dunning state at that moment. (``serving_status`` is read only by the
            # channel senders, which are never dormant — D1.)
            if not (c.status == "dormant" and c.status_stashed is not None):
                c.status_stashed = c.status
                c.status_detail_stashed = c.status_detail
            c.status = QUIESCING
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
        if src.soft_deleted_at is None:
            # A source the owner already deleted keeps its own tombstone + clock; the
            # Luciel delete only tombstones what was live (audit F024).
            src.soft_deleted_at = _now()
            src.deleted_by = "lifecycle"

    # 3. state → grace window.
    inst.state = LucielState.luciel_grace_window
    inst.grace_window_started_at = _now()
    inst.soft_deleted_at = _now()
    await session.flush()
    await _emit_transition(
        session, inst, actor_user_id=actor_user_id, frm=frm, to="luciel_grace_window"
    )
    return inst


async def _requalify_crm_links(
    session: AsyncSession,
    inst: Instance,
    *,
    conns: list[InstanceConnection],
    intact_providers: set[str],
    actor_user_id: uuid.UUID | None,
) -> int:
    """Forget the CRM link rows that no longer name a record in the tenant's CRM
    (round 7 WP-5, item 8).

    ``crm_lead_links`` pins lead → CRM record id, and a record id belongs to ONE CRM
    account. The links ride through the grace window untouched (the day-30 cascade
    destroys them, restore needs them), so restore is where they must be re-qualified
    against the connection that actually came back. They stay ONLY for a provider
    whose CRM connection is revived on the credential the delete put away
    (``intact_providers``). Everything else is a possibly-different account — the
    owner reconnected the CRM during grace (a fresh consent on the draining row, or a
    new row that superseded it), or the row restores ``unconfigured`` and must be
    reconnected — and a stale id there does not merely 404-heal: it PATCHes whatever
    contact carries that id in the other portal. Cleared exactly as a disconnect or a
    swap clears them (``connections.service.clear_crm_links``): the link rows go, and
    with them the leads' CRM push stamps (``connections.service.reset_crm_push_state``,
    round 7 phase 2 seams 7). The next push relinks by the documented search-by-key
    step. Audited with counts, never a lead key."""
    stmt = delete(CrmLeadLink).where(
        CrmLeadLink.admin_id == inst.admin_id, CrmLeadLink.instance_id == inst.id
    )
    if intact_providers:
        stmt = stmt.where(CrmLeadLink.provider.not_in(sorted(intact_providers)))
    result = await session.execute(stmt)
    cleared = int(getattr(result, "rowcount", 0) or 0)
    leads_reset = 0
    if not intact_providers:
        # The per-lead stamps go with the links. ``Lead`` has no provider column and
        # CRM is single-active per type, so the stamps are kept exactly when a CRM
        # connection came back intact (they then describe the account that IS
        # connected) and reset otherwise — keyed on the connection, not on how many
        # link rows happened to exist. Left stamped ``synced``, a lead read "in your
        # CRM" against an account it was never pushed to, was skipped by the one-time
        # backfill into the new account (``crm_push_status IS NULL``), and still
        # counted in the "leads to CRM" numbers.
        from app.connections.service import reset_crm_push_state  # noqa: PLC0415

        leads_reset = await reset_crm_push_state(
            session, admin_id=inst.admin_id, instance_id=inst.id
        )
    if cleared or leads_reset:
        current = next(
            (
                c
                for c in conns
                if c.connection_type == "crm" and c.status not in NOT_SERVING_STATUSES
            ),
            None,
        )
        await audit.emit(
            session,
            admin_id=inst.admin_id,
            instance_id=inst.id,
            actor_type=ActorType.admin_user if actor_user_id else ActorType.platform,
            actor_user_id=actor_user_id,
            event_type="crm_links_cleared",
            payload={
                "connection_id": str(current.connection_id) if current else None,
                "provider": current.provider if current else None,
                "count": cleared,
                "leads_reset": leads_reset,
                "reason": "restore_crm_connection_changed",
            },
        )
    return cleared


async def _remove_grace_window_duplicates(
    session: AsyncSession,
    inst: Instance,
    sources: list[KnowledgeSource],
) -> int:
    """Destroy the second copy of a synced document that a sync wrote DURING the
    delete (round 7 WP-5, item 8).

    A knowledge sync whose provider fetch is on the wire when the owner deletes the
    Luciel finishes against sources the delete tombstoned a moment earlier. It reads
    "nothing is synced yet" and ingests every document again — live rows on a deleted
    Luciel, same ``(connection_id, external_id)`` as the tombstoned originals. Restore
    revives the originals ("exactly as it was", §3.6.4: their ids are what traces and
    the dashboard know), so the grace-window copy must go, or every document exists
    twice and the sync engine — which keys on ``external_id`` — tracks one and leaves
    the other serving stale content forever. Hard-deleted rather than tombstoned: the
    owner never had it, so there is no undo to offer. Call BEFORE the originals are
    revived. Returns how many copies were removed."""
    coming_back = {
        (s.connection_id, s.external_id)
        for s in sources
        if s.deleted_by == "lifecycle" and s.connection_id is not None and s.external_id
    }
    duplicates = [
        s
        for s in sources
        if s.soft_deleted_at is None and (s.connection_id, s.external_id) in coming_back
    ]
    if not duplicates:
        return 0
    ids = [s.id for s in duplicates]
    # Edges before entities (they reference them, §3.2.1 rule 5); chunks any time.
    for model in (KnowledgeChunk, KnowledgeEdge, KnowledgeEntity):
        await session.execute(
            delete(model).where(model.admin_id == inst.admin_id, model.source_id.in_(ids))
        )
    for dup in duplicates:
        await session.delete(dup)
    await session.flush()
    await audit.emit(
        session,
        admin_id=inst.admin_id,
        instance_id=inst.id,
        actor_type=ActorType.platform,
        event_type="knowledge_source_edited",
        payload={
            "action": "restore_duplicates_removed",
            "count": len(duplicates),
            "connection_ids": sorted({str(s.connection_id) for s in duplicates}),
        },
    )
    return len(duplicates)


async def restore(session: AsyncSession, admin_id: uuid.UUID, actor_user_id: uuid.UUID) -> Instance:
    """Restore within grace → ACTIVE (not paused) (Architecture §3.6.4).

    The instance row is locked for the transaction: the day-30 grace-expiry sweep
    takes the same lock, so a restore on the last day and the sweep cannot both act
    on a Luciel each read as "still in its grace window"."""
    inst = await _get_instance(session, admin_id, for_update=True)
    if inst.state != LucielState.luciel_grace_window:
        raise LifecycleError("not_in_grace_window")
    inst.state = LucielState.active
    inst.grace_window_started_at = None
    inst.soft_deleted_at = None
    # Revive the SAME embed key the delete stashed ("exactly as it was",
    # §3.6.4): the snippet already deployed on the customer's site starts
    # working again with no re-edit. Minting fresh is only the fallback for a
    # legacy row that entered grace before the stash column existed.
    if inst.embed_key_stashed:
        inst.embed_key_public_id = inst.embed_key_stashed
    else:
        from app.lifecycle.embed_keys import assign_fresh_embed_key  # noqa: PLC0415

        await assign_fresh_embed_key(session, inst)  # item 14: unique, retried
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
    #      rows went quiescing — a draining row routes inbound but holds no claim).
    #      Restoring the claim would capture their inbound — so a foreign-held
    #      destination is STRIPPED and the row comes back unconfigured with an
    #      honest detail, never silently re-routed.
    #   2. If this tenant re-attached the same connection TYPE during grace (a new
    #      live row exists), reviving the old row would violate the
    #      single-active-per-type unique index — the superseded row stays as it is.
    # Both a ``quiescing`` row (what delete writes since WP-5) and a ``revoked`` one
    # (a pre-WP-5 delete) are revived here — NOT_SERVING_STATUSES is the one set the
    # delete, the restore and the cascade all agree on.
    from app.channels import routing  # noqa: PLC0415 — circular at module import

    active_types = {c.connection_type for c in conns if c.status not in NOT_SERVING_STATUSES}
    # CRM providers whose connection comes back on the credential the delete put away
    # — the only ones whose ``crm_lead_links`` still name records in the right account.
    intact_crm_providers: set[str] = set()
    for c in conns:
        if c.status not in NOT_SERVING_STATUSES:
            continue
        if c.connection_type in active_types:
            # Superseded during grace; the newer row is the live one. A DRAINING
            # row must not linger on an active Luciel (the resolver would keep
            # routing on it) — it goes terminal, exactly where a pre-WP-5 delete
            # left it. Its secret is destroyed by the tenant prefix sweep at the
            # account cascade, as any revoked row's is. Defensive: ``uq_conn_active_type``
            # still counts a quiescing row as the type's single active slot, so a
            # re-attach during grace REUSES the draining row (connections.service
            # reads any non-revoked row) rather than adding a second one; only a
            # many-active type (knowledge_source) or hand-edited data reaches here.
            if c.status == QUIESCING:
                c.status = "revoked"
                clear_sync_job_on_removal(c)
                c.status_stashed = None
                c.status_detail_stashed = None
            continue
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
            c.status = "unconfigured"
            c.status_detail = (
                "While this Luciel was deleted, this number or address was connected "
                "to a different account — designate a new one to bring the channel back."
            )
        else:
            # §3.6.4 (C8): restore to the PRE-DELETE status the delete stashed —
            # "exactly as it was". Secrets were never destroyed during grace, so a
            # previously-connected row is genuinely connected again; the health
            # sweep re-proves every non-revoked row on its normal cadence, and a
            # credential that died during grace degrades to the honest Reconnect
            # chip (§3.8.5) rather than everything restoring as "reconnect all".
            # Legacy rows that entered grace before 0049 have no stash → the old
            # conservative 'unconfigured'.
            if c.connection_type == "crm" and c.status_stashed:
                intact_crm_providers.add(c.provider)
            c.status = c.status_stashed or "unconfigured"
            c.status_detail = c.status_detail_stashed
        c.status_stashed = None
        c.status_detail_stashed = None
    await _requalify_crm_links(
        session,
        inst,
        conns=list(conns),
        intact_providers=intact_crm_providers,
        actor_user_id=actor_user_id,
    )
    # Billing dormancy is re-derived, not replayed (round 7 money review, finding 2):
    # the delete kept each action tool's PRE-dormant stash, so the rows above came
    # back at their real status. If dunning is still ``reduced`` they go dormant again
    # (stashing that status for the payment that restores them); if the invoice was
    # paid during the grace window they stay as they were — a payment then found the
    # rows quiescing and had nothing to restore.
    dunning_state = (
        await session.execute(
            select(BillingAccount.dunning_state).where(BillingAccount.admin_id == admin_id)
        )
    ).scalar_one_or_none()
    if dunning_state is not None and dunning.behaves_as_free_cap(dunning_state):
        await dunning.set_dormant_for_action_tools(
            session, admin_id=admin_id, reason="dunning_reduced"
        )
    sources = list(
        (await session.execute(select(KnowledgeSource).where(KnowledgeSource.admin_id == admin_id)))
        .scalars()
        .all()
    )
    await _remove_grace_window_duplicates(session, inst, sources)
    for src in sources:
        # Only what the Luciel delete tombstoned comes back with it. A source the
        # owner deleted deliberately stays deleted (its own undo window and purge
        # apply) — reviving it here handed back knowledge with nothing behind it
        # (audit F024).
        if src.deleted_by == "lifecycle":
            src.soft_deleted_at = None
            src.deleted_by = None
    await session.flush()
    await _emit_transition(
        session, inst, actor_user_id=actor_user_id, frm="luciel_grace_window", to="active"
    )
    return inst
