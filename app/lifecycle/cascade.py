"""Hard-delete cascade + account closure (Architecture §3.6.5 / §3.6.6).

The Luciel hard-delete cascade fires at day 30 of the grace window (or on an
explicit skip-grace confirm). Ordered to respect FK dependencies and to LOG each
data class deletion (``data_retention_hard_delete``) BEFORE the rows are gone.
It destroys the Luciel and everything attached, but NOT the account shell and NOT
the admin_audit_log (the proof of deletion is retained, §3.6.5).

Two scopes (audit F014/F019). The LUCIEL scope is everything the Luciel owns; the
ACCOUNT scope adds the billing identity (``billing_accounts`` + the Stripe replay
gate), which belongs to the account shell, not the Luciel: a hard-deleted Luciel
leaves an account that still has its card on file, its lifetime count, and its
Stripe customer — the owner may create a new Luciel against it. Only the account
purge (closure day 30, unverified reap) destroys that scope, and it retires the
Stripe customer object first so the processor holds no identity for a tenant that
no longer exists.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.billing import metering
from app.billing.stripe_gateway import StripeGateway, get_stripe_gateway
from app.connections.secrets import get_secret_store, tenant_secret_prefix
from app.db.base import TenantMixin
from app.lifecycle import state_machine
from app.lifecycle.export_bundle import hold_export_lock
from app.lifecycle.export_store import export_prefix_for, get_export_store
from app.lifecycle.transcript_store import get_transcript_store, transcript_prefix_for
from app.models.audit import ActorType, AdminAuditLog
from app.models.billing import BillingAccount, ProcessedStripeEvent
from app.models.booking import BookingEvent
from app.models.budget import ConversationBudget, CountedSession
from app.models.connection import InstanceConnection
from app.models.conversation import EscalationEvent, Session, SessionSummary, Transcript
from app.models.crm_link import CrmLeadLink
from app.models.deferred import DeferredAction
from app.models.email_incident import EmailDeliveryIncident
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.knowledge import KnowledgeChunk, KnowledgeEdge, KnowledgeEntity, KnowledgeSource
from app.models.lead import Lead, LeadIdentifier
from app.models.notice_delivery import NoticeDelivery
from app.models.record_source import RecordSourceRecord
from app.models.tool_auth import InstanceToolAuthorization
from app.models.trace import RetrievalTraceEvent, ToolTraceEvent
from app.models.voice import VoiceCallRecord

_log = logging.getLogger(__name__)

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
LUCIEL_SCOPE_MODELS: tuple[tuple[type[TenantMixin], str], ...] = (
    (KnowledgeChunk, "knowledge_chunk"),
    (KnowledgeEdge, "knowledge_edge"),
    (KnowledgeEntity, "knowledge_entity"),
    (KnowledgeSource, "knowledge_source"),
    (Transcript, "transcript"),
    (SessionSummary, "session_summary"),
    (LeadIdentifier, "lead_identifier"),
    (Lead, "lead_record"),
    (EscalationEvent, "escalation_event"),
    (ToolTraceEvent, "tool_trace_event"),
    (RetrievalTraceEvent, "retrieval_trace_event"),
    (VoiceCallRecord, "voice_call_record"),
    (CrmLeadLink, "crm_lead_link"),
    (EmailDeliveryIncident, "email_delivery_incident"),
    (RecordSourceRecord, "record_source_record"),
    (BookingEvent, "booking_event"),
    (DeferredAction, "deferred_action"),
    (InstanceToolAuthorization, "instance_tool_authorization"),
    (ConversationBudget, "conversation_budget"),
    (CountedSession, "counted_session"),
    (NoticeDelivery, "notice_delivery"),
    (InstanceConnection, "instance_connection"),
    (Session, "session"),
    (Instance, "luciel_instance"),
)

#: The billing identity: destroyed only with the account shell (see module docstring).
ACCOUNT_SCOPE_MODELS: tuple[tuple[type[TenantMixin], str], ...] = (
    (ProcessedStripeEvent, "processed_stripe_event"),
    (BillingAccount, "billing_account"),
)

#: Everything tenant deletion can ever destroy — the completeness test's registry.
CASCADE_MODELS: tuple[tuple[type[TenantMixin], str], ...] = (
    LUCIEL_SCOPE_MODELS + ACCOUNT_SCOPE_MODELS
)


async def _log_and_delete(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID | None,
    model: type[TenantMixin],
    data_class: str,
) -> int:
    """Log a data_retention_hard_delete for the class, then delete its rows.

    The count is taken before the delete so the audit row — written BEFORE the rows
    are gone, as the module docstring promises — can still say how many (round 7
    WP-5, item 6: the owner-facing log renders ``data_class: N deleted``)."""
    count = (
        await session.execute(
            select(func.count()).select_from(model).where(model.admin_id == admin_id)
        )
    ).scalar_one()
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        event_type="data_retention_hard_delete",
        actor_type=ActorType.platform,
        payload={
            "data_class": data_class,
            "count": int(count),
            "retention_policy_applied": "luciel_hard_delete_cascade",
        },
    )
    result = await session.execute(delete(model).where(model.admin_id == admin_id))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def _delete_tenant_secrets(
    session: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID | None
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
            # An ERROR line, not only an audit payload nobody queries (audit F041):
            # this is what the sweep/app error alarms read.
            _log.exception(
                "cascade: secret delete failed admin_id=%s connection=%s",
                admin_id,
                conn.connection_id,
            )
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
        _log.exception("cascade: secret prefix sweep failed admin_id=%s", admin_id)
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


async def _delete_tenant_transcript_archive(
    session: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID | None
) -> None:
    """Destroy the tenant's archived-transcript objects (Commitment #38).

    The cascade deletes the Postgres rows, but a session archived at the 90-day
    boundary keeps its transcript in the S3 cold store — left alone, a deleted
    tenant's conversations would survive up to ~a year past "hard-deleted" (until
    the bucket's lifecycle backstop fired). One ``vm/{admin_id}/transcripts/``
    prefix sweep removes them all, mirroring the Secrets Manager prefix cleanup
    above: best-effort (a store outage must not abort a deletion the owner already
    confirmed — the bucket backstop still bounds the objects), with the outcome
    recorded on the audit trail either way.
    """
    prefix = transcript_prefix_for(admin_id)
    try:
        swept = await get_transcript_store().delete_prefix(prefix=prefix)
        error: str | None = None
    except Exception as exc:  # noqa: BLE001 — a broken store must not strand the deletion
        swept, error = 0, type(exc).__name__
        _log.exception("cascade: transcript archive sweep failed admin_id=%s", admin_id)
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        actor_type=ActorType.platform,
        event_type="transcript_archive_cleanup",
        payload={
            "object_prefix": prefix,
            "deleted_count": swept,
            "error": error,
            "context": "hard_delete_cascade_prefix_sweep",
        },
    )


async def _retire_stripe_customer(
    session: AsyncSession,
    admin_id: uuid.UUID,
    *,
    context: str,
    gateway: StripeGateway | None = None,
) -> None:
    """Destroy the tenant's Stripe customer before the row that names it is deleted
    (§3.6.6 / Legal §B5). Best-effort with the outcome audited: a processor outage
    must not strand a purge the owner already confirmed, and the audit row is what
    an operator uses to finish the job by hand."""
    account = (
        await session.execute(select(BillingAccount).where(BillingAccount.admin_id == admin_id))
    ).scalar_one_or_none()
    if account is None or account.stripe_customer_id is None:
        return
    gateway = gateway or get_stripe_gateway()
    try:
        deleted = await gateway.delete_customer(customer_id=account.stripe_customer_id)
        error: str | None = None
    except Exception as exc:  # noqa: BLE001 — a broken processor must not strand the purge
        deleted, error = False, type(exc).__name__
        _log.exception("stripe customer retirement failed admin_id=%s", admin_id)
    await audit.emit(
        session,
        admin_id=admin_id,
        actor_type=ActorType.platform,
        event_type="stripe_customer_deleted",
        payload={
            "customer_id": account.stripe_customer_id,
            "subscription_id": account.stripe_subscription_id,
            "deleted": deleted,
            "error": error,
            "context": context,
        },
    )


async def _delete_tenant_knowledge_originals(
    session: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID | None
) -> None:
    """Destroy the tenant's original uploaded files (F016 store) by prefix — same
    best-effort-with-audit posture as the transcript archive sweep above."""
    from app.knowledge.original_store import (  # noqa: PLC0415 — avoid import cycle
        get_original_store,
        original_prefix_for,
    )

    prefix = original_prefix_for(admin_id)
    try:
        swept = await get_original_store().delete_prefix(prefix=prefix)
        error: str | None = None
    except Exception as exc:  # noqa: BLE001 — a broken store must not strand the deletion
        swept, error = 0, type(exc).__name__
        _log.exception("cascade: knowledge originals sweep failed admin_id=%s", admin_id)
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        actor_type=ActorType.platform,
        event_type="data_retention_hard_delete",
        payload={
            "data_class": "knowledge_original_object",
            "object_prefix": prefix,
            "count": swept,
            "error": error,
            "retention_policy_applied": "luciel_hard_delete_cascade",
        },
    )


async def _delete_tenant_export_bundles(
    session: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID | None
) -> None:
    """Destroy the tenant's export ZIPs (§3.6.6 / §5.10) by prefix (round 7 WP-5,
    item 4). A bundle is every table the tenant owns in one object; the cascade
    below deletes those tables, and a bundle left behind would keep a full copy of
    the data past "hard-deleted" for as long as the bucket's lifecycle backstop let
    it. Same best-effort-with-audit posture as the three sibling sweeps above."""
    prefix = export_prefix_for(admin_id)
    try:
        swept = await get_export_store().delete_prefix(prefix=prefix)
        error: str | None = None
    except Exception as exc:  # noqa: BLE001 — a broken store must not strand the deletion
        swept, error = 0, type(exc).__name__
        _log.exception("cascade: export bundle sweep failed admin_id=%s", admin_id)
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        actor_type=ActorType.platform,
        event_type="data_retention_hard_delete",
        payload={
            "data_class": "export_bundle_object",
            "object_prefix": prefix,
            "count": swept,
            "error": error,
            "retention_policy_applied": "luciel_hard_delete_cascade",
        },
    )


async def hard_delete_cascade(
    session: AsyncSession,
    admin_id: uuid.UUID,
    *,
    include_account_scope: bool = False,
    gateway: StripeGateway | None = None,
) -> dict[str, int]:
    """Run the ordered cascade for the tenant's Luciel. Returns per-table row counts.

    Destroys every table in ``LUCIEL_SCOPE_MODELS`` (child data first, instance row
    last) plus the tenant's secret material. The account shell and the admin_audit_log
    survive (§3.6.5). With ``include_account_scope`` — the account PURGE paths only —
    the billing identity goes too, after the Stripe customer is retired.

    A tenant with no Instance row is still cascaded. The Luciel is not what makes the
    other rows tenant data: an account can attach a card, connect Google, and close
    without ever finishing onboarding, and the closure path is the only caller that
    would ever remove those rows. Bailing early left them — and live OAuth credentials —
    under an admin_id no query would ever name again.
    """
    # FIRST, before any read or write: wait out an export that is still uploading, so
    # the bundle sweep below destroys what it wrote instead of running a moment too
    # early and leaving a full copy of the tenant behind (round 7 WP-5, item 8).
    await hold_export_lock(session, admin_id)
    inst = (
        await session.execute(select(Instance).where(Instance.admin_id == admin_id))
    ).scalar_one_or_none()
    iid = inst.id if inst is not None else None
    await _delete_tenant_secrets(session, admin_id, iid)
    await _delete_tenant_transcript_archive(session, admin_id, iid)
    await _delete_tenant_knowledge_originals(session, admin_id, iid)
    await _delete_tenant_export_bundles(session, admin_id, iid)

    if inst is not None:
        inst.state = LucielState.luciel_hard_deleted
    await session.flush()
    models = LUCIEL_SCOPE_MODELS
    if include_account_scope:
        await _retire_stripe_customer(
            session, admin_id, context="hard_delete_cascade", gateway=gateway
        )
        models = models + ACCOUNT_SCOPE_MODELS
    counts = {
        # __tablename__ lives on each concrete model, not on the TenantMixin bound.
        str(getattr(model, "__tablename__")): await _log_and_delete(  # noqa: B009
            session, admin_id, iid, model, data_class
        )
        for model, data_class in models
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


async def settle_billing_at_closure(
    session: AsyncSession,
    admin_id: uuid.UUID,
    *,
    gateway: StripeGateway | None = None,
) -> dict[str, object]:
    """Charge what the tenant already ran, then end the metered subscription (Legal §A4).

    Closure is the last moment the card is reliably on file. The accrued-but-unreported
    residual is flushed to the meter first (the same path card detach uses), then the
    subscription is cancelled WITH a final invoice for the partial period so the last
    blocks are actually collected — before this the subscription was simply abandoned
    and the residual quietly forgiven (audit F001/F002). Best-effort against Stripe with
    the outcome audited; the day-30 purge retires the customer regardless.
    """
    account = (
        await session.execute(select(BillingAccount).where(BillingAccount.admin_id == admin_id))
    ).scalar_one_or_none()
    outcome: dict[str, object] = {
        "subscription_id": None,
        "usage_reported_blocks": 0,
        "usage_owed": False,
        "usage_unreported": False,
        "subscription_cancelled": None,
        "error": None,
    }
    if account is None:
        return outcome
    gateway = gateway or get_stripe_gateway()
    outcome["subscription_id"] = account.stripe_subscription_id
    try:
        reported = await metering.report_accrued_usage(session, admin_id=admin_id, gateway=gateway)
        outcome["usage_reported_blocks"] = reported
        outcome["usage_owed"] = account.accrued_blocks > 0
    except Exception as exc:  # noqa: BLE001 — closure must complete; the failure is audited
        outcome["error"] = type(exc).__name__
        outcome["usage_owed"] = account.accrued_blocks > 0
        _log.exception("closure usage flush failed admin_id=%s", admin_id)
    # Adversarial review R-WP1-3: "owed" only says usage existed. The operator who
    # finishes a failed settlement by hand needs to know whether any of it is STILL
    # unreported after the flush — that is the number Stripe has not seen.
    outcome["usage_unreported"] = account.accrued_blocks > account.last_reported_blocks
    if account.stripe_subscription_id is not None:
        try:
            cancelled = await gateway.cancel_subscription(
                subscription_id=account.stripe_subscription_id, invoice_now=True
            )
            outcome["subscription_cancelled"] = cancelled
            if cancelled:
                account.stripe_subscription_id = None
        except Exception as exc:  # noqa: BLE001
            outcome["subscription_cancelled"] = False
            outcome["error"] = type(exc).__name__
            _log.exception("closure subscription cancel failed admin_id=%s", admin_id)
    await session.flush()
    await audit.emit(
        session,
        admin_id=admin_id,
        actor_type=ActorType.platform,
        event_type="billing_closure_settled",
        payload={**outcome, "invoice_now": True},
    )
    return outcome


async def closure_record(session: AsyncSession, admin_id: uuid.UUID) -> dict[str, object]:
    """What the CURRENT closure did, from its ``account_closure_initiated`` audit row
    (the latest one — an account can close more than once since D5).

    Carries ``luciel_state_before`` and ``luciel_deleted_by_closure``. Empty for a
    closure recorded before those keys existed: the reopen then restores nothing it
    cannot prove the closure took, and the Luciel's own Restore (same 30-day window)
    is still there for the owner."""
    payload = (
        await session.execute(
            select(AdminAuditLog.event_payload)
            .where(
                AdminAuditLog.admin_id == admin_id,
                AdminAuditLog.event_type == "account_closure_initiated",
            )
            .order_by(AdminAuditLog.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return dict(payload) if isinstance(payload, dict) else {}


async def close_account(
    session: AsyncSession,
    admin_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    *,
    gateway: StripeGateway | None = None,
) -> None:
    """Close the account shell (Architecture §3.6.6) — the nested model.

    Closure SUBSUMES deletion, it does not skip it: a Luciel that is still serving is
    put through the same fused-quiesce delete every owner gets (embed key stashed,
    connections revoked with their status stashed, knowledge soft-deleted, 30-day grace
    window stamped), so the closure is recoverable for the window Legal §B5 promises
    and the in-flight conversations drain (§3.6.3). The hard cascade runs at day 30 —
    from the grace-expiry sweep or the closure purge, whichever comes first. Synchronous
    destruction here (audit F001) contradicted the product's own copy.

    Outstanding pay-as-you-go usage is settled at closure while the card is still on
    file (Legal §A4). Audit tombstones are retained.

    Owner decision D5 (2026-09-14): the closure can be REOPENED for 30 days. Two things
    are recorded here for that: ``accounts.closed_at`` (the reopen / purge clock — an
    account can now close more than once, so the first-ever closure audit row is no
    longer a usable clock) and, on the ``account_closure_initiated`` audit row, what
    the closure did to the Luciel (``luciel_state_before`` + ``luciel_deleted_by_closure``)
    so the reopen restores exactly that and nothing the owner had deleted on purpose.
    """
    inst = (
        await session.execute(select(Instance).where(Instance.admin_id == admin_id))
    ).scalar_one_or_none()
    luciel_state_before = inst.state.value if inst is not None else None
    deleted_by_closure = inst is not None and inst.state in (
        LucielState.active,
        LucielState.paused,
    )
    closed_at = datetime.now(UTC)
    if deleted_by_closure:
        deleted = await state_machine.delete_luciel(session, admin_id, actor_user_id)
        # ONE instant for both clocks. The Luciel's grace window and the account's
        # reopen window must end together: a grace window stamped a moment earlier
        # would let the grace-expiry sweep destroy the Luciel while the reopen that
        # promises to bring it back is still on offer.
        closed_at = deleted.grace_window_started_at or closed_at
    # Already in the grace window or hard-deleted: nothing more to quiesce.

    await settle_billing_at_closure(session, admin_id, gateway=gateway)

    await audit.emit(
        session,
        admin_id=admin_id,
        event_type="account_closure_initiated",
        actor_type=ActorType.admin_user,
        actor_user_id=actor_user_id,
        payload={
            "luciel_state_before": luciel_state_before,
            "luciel_deleted_by_closure": deleted_by_closure,
        },
    )
    acct = (
        await session.execute(select(Account).where(Account.id == admin_id))
    ).scalar_one_or_none()
    if acct is not None:
        if acct.state != AccountState.closed:
            # A repeated close must not restart the 30-day clock.
            acct.closed_at = closed_at
        acct.state = AccountState.closed
        # Revoke every live session (P1-5). The account row outlives closure by 30 days
        # (§3.6.6), so without the bump a cookie minted before closure stays valid against
        # the whole admin surface until it happens to expire — on every device, not just
        # the one that clicked close.
        acct.session_epoch += 1
    await session.flush()
    await audit.emit(
        session,
        admin_id=admin_id,
        event_type="lifecycle_transition",
        actor_type=ActorType.admin_user,
        actor_user_id=actor_user_id,
        payload={
            "from": "luciel_grace_window" if inst is not None else "account:empty",
            "to": "account_closing",
        },
    )
