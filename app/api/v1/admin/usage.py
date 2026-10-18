"""Budget monitoring + usage dashboard API (DOCTRINE ANCHOR — Architecture §8).

Serves the frozen client (http-admin.ts analytics.*):
  * GET /api/v1/admin/usage/overview   → analyticsOverview (aggregates, §3.9)
  * GET /api/v1/admin/usage/audit-log  → auditEvent[] (read-only, hash-chained §5.2)
  * GET /api/v1/admin/usage/export.csv → CSV download of any analytics view for the
    selected period (Vision §7: "exportable reports — CSV download of any
    analytics view"), available to ALL accounts (no plan gate).

Both are admin-authed (verified, §3.7.2), run inside ``tenant_session`` so RLS
scopes every aggregate to the caller's tenant (no new PII), and read existing
stores only — no new write path. The audit-log view reuses the Arc 9/10
hash-chained log and its ``verify_chain`` (read-only, non-secret).

Real aggregate queries live in app/analytics/service.py (this router is a thin
HTTP layer over it) — see that module's docstring for the honest-gap policy on
appointments-booked / response-time-percentiles / top-knowledge-sources.
"""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import service as analytics_service
from app.api.deps import Subject, current_admin
from app.billing import metering
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.instance import Instance
from app.notifications.escalation_dispatch import has_configured_escalation_contact
from app.runtime.budget import resolve_billing_period_start
from app.schemas_api.usage import (
    AnalyticsOverviewOut,
    AuditEventOut,
    ChannelConversionOut,
    ChannelMix,
    EscalationBySignal,
    SourceConversionOut,
    TopKnowledgeSource,
)

router = APIRouter(prefix="/api/v1/admin/usage", tags=["usage"])


async def _instance_id(s: AsyncSession, admin_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
    ).scalar_one_or_none()


async def _build_overview_out(
    s: AsyncSession, subject: Subject
) -> tuple[AnalyticsOverviewOut, analytics_service.AnalyticsAggregate]:
    instance_id = await _instance_id(s, subject.admin_id)

    # conversationsThisPeriod comes from the budget counter (the single source
    # of the count, §3.4.1b) — NOT recomputed from sessions.
    period = await resolve_billing_period_start(s, admin_id=subject.admin_id)
    conversations_this_period = 0
    budget_util = 0.0
    if instance_id is not None:
        budget = await metering.build_budget(s, admin_id=subject.admin_id, instance_id=instance_id)
        conversations_this_period = budget.conversations_this_period
        # Utilization against the free allowance (≥0; can exceed 1 on PAYG).
        budget_util = (
            budget.conversations_this_period / budget.free_allowance
            if budget.free_allowance
            else 0.0
        )

    agg = await analytics_service.build_overview(
        s,
        admin_id=subject.admin_id,
        period_start=period,
        conversations_this_period=conversations_this_period,
    )

    # harmony fix 2026-08-04 item 4: surface whether escalations have anywhere real to
    # go, so the FE can nudge the owner instead of assuming the ack's promise held.
    escalation_config = None
    if instance_id is not None:
        escalation_config = (
            await s.execute(select(Instance.escalation).where(Instance.id == instance_id))
        ).scalar_one_or_none()
    escalation_contact_configured = has_configured_escalation_contact(escalation_config)
    # Round 5B item 13: is a configured email contact currently suppressed?
    escalation_contact_bouncing = False
    if escalation_config:
        from app.models.email_incident import is_suppressed  # noqa: PLC0415
        from app.notifications.delivery_incidents import states_for  # noqa: PLC0415

        contacts = [
            str(a)
            for a in (
                escalation_config.get("primaryEmail"),
                escalation_config.get("secondaryEmail"),
            )
            if a
        ]
        if contacts:
            states = await states_for(s, subject.admin_id, contacts)
            escalation_contact_bouncing = any(
                is_suppressed(states.get(c.strip().lower())) for c in contacts
            )

    out = AnalyticsOverviewOut.model_validate(
        {
            "conversationsThisPeriod": agg.conversations_this_period,
            "conversationsTotal": agg.conversations_total,
            "leadsThisPeriod": agg.leads_this_period,
            "appointmentsBooked": agg.appointments_booked,
            "appointmentsBookedNote": (
                agg.appointments_booked_gap.reason if agg.appointments_booked_gap else None
            ),
            "responseTimeP50Seconds": agg.response_time_p50_seconds,
            "responseTimeP95Seconds": agg.response_time_p95_seconds,
            "responseTimeNote": agg.response_time_gap.reason if agg.response_time_gap else None,
            "escalationsBySignal": [
                EscalationBySignal.model_validate({"signal": e.signal, "count": e.count})
                for e in agg.escalations_by_signal
            ],
            "channelMix": [
                ChannelMix.model_validate({"channel": c.channel, "fraction": c.fraction})
                for c in agg.channel_mix
            ],
            "budgetUtilization": budget_util,
            "busiestTimes": agg.busiest_times,
            "topKnowledgeSources": [
                TopKnowledgeSource.model_validate(
                    {
                        "sourceId": src["source_id"],
                        "name": src["name"],
                        "retrievalCount": src["retrieval_count"],
                    }
                )
                for src in agg.top_knowledge_sources
            ],
            "topKnowledgeSourcesNote": (
                agg.top_knowledge_sources_gap.reason if agg.top_knowledge_sources_gap else None
            ),
            "conversionByChannel": [
                ChannelConversionOut.model_validate(
                    {
                        "channel": c.channel,
                        "conversations": c.conversations,
                        "leads": c.leads,
                        "conversionRate": c.conversion_rate,
                    }
                )
                for c in agg.conversion_by_channel
            ],
            "conversionBySource": [
                SourceConversionOut.model_validate(
                    {
                        "source": c.source,
                        "leads": c.leads,
                        "converted": c.converted,
                        "conversionRate": c.conversion_rate,
                    }
                )
                for c in agg.conversion_by_source
            ],
            "conversionBySourceNote": (
                agg.conversion_by_source_gap.reason if agg.conversion_by_source_gap else None
            ),
            "conversionByServiceNote": (
                agg.conversion_by_service_gap.reason if agg.conversion_by_service_gap else None
            ),
            "escalationContactConfigured": escalation_contact_configured,
            "escalationContactBouncing": escalation_contact_bouncing,
        }
    )
    return out, agg


@router.get("/overview", response_model=AnalyticsOverviewOut)
async def usage_overview(subject: Subject = Depends(current_admin)) -> AnalyticsOverviewOut:
    async with tenant_session(subject.admin_id) as s:
        out, _agg = await _build_overview_out(s, subject)
        return out


# A payload value is rendered into ``detail`` ONLY when it is code-shaped: a state
# name, a policy id, a count, an ISO instant. A provider's sentence (a ``status_detail``
# quoting the carrier, a refresh error naming the mailbox) never matches, so the detail
# cannot carry a phone number, an address or a token by accident (§3.8.3 / §3.4.11).
_CODE = re.compile(r"^[A-Za-z0-9_:.+-]{1,64}$")


def _code(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and _CODE.fullmatch(value):
        return value
    return None


def _transition(payload: dict[str, object], *, note_key: str) -> str | None:
    frm, to = _code(payload, "from"), _code(payload, "to")
    if frm is None or to is None:
        return None
    note = _code(payload, note_key)
    return f"{frm} → {to}" + (f" ({note})" if note else "")


def _count(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    return len(value) if isinstance(value, list) else 0


def _knowledge_edit(payload: dict[str, object]) -> str | None:
    """A sync row renders COUNTS (round 7 Phase 2 review, knowledge #4): its lists are
    external ids — a crawl's are URLs — and never reach the detail. A page the quota
    refused used to leave nothing the owner could see but a bare event name."""
    action = _code(payload, "action")
    if action == "sync":
        head = (
            f"sync: {_count(payload, 'added')} added, {_count(payload, 'updated')} updated, "
            f"{_count(payload, 'removed')} removed, {_count(payload, 'refused')} refused"
        )
        unreached = _count(payload, "unreached")
        return head + (f", {unreached} not reached" if unreached else "")
    if action == "sync_refused_shrink":
        synced, reported = _code(payload, "synced"), _code(payload, "reported")
        if synced is None or reported is None:
            return "sync refused (shrink)"
        return f"sync refused (shrink): {synced} synced, {reported} reported"
    if action == "sync_failed":
        error = _code(payload, "error")
        return f"sync failed ({error})" if error else "sync failed"
    return action


def audit_detail(event_type: str, payload: dict[str, object]) -> str | None:
    """The one-line, owner-readable detail for an audit row (round 7 WP-5, item 6).

    The log used to surface ``event_type`` alone, so fifty ``lifecycle_transition``
    rows were indistinguishable and a ``data_retention_hard_delete`` named neither its
    data class nor its count. Rendered from a per-event ALLOW-LIST of code-shaped
    values (see ``_CODE``) — never a secret, never a value that is PII; an event type
    with no renderer has no detail whatever its payload holds."""
    if event_type == "lifecycle_transition":
        return _transition(payload, note_key="trigger")
    if event_type == "dunning_state_changed":
        return _transition(payload, note_key="reason")
    if event_type in ("data_retention_hard_delete", "lead_pruned"):
        policy = _code(payload, "retention_policy_applied")
        if event_type == "lead_pruned":
            return f"lead pruned ({policy})" if policy else None
        data_class = _code(payload, "data_class")
        if data_class is None:
            return None
        count = _code(payload, "count")
        head = f"{data_class}: {count} deleted" if count is not None else data_class
        return f"{head} ({policy})" if policy else head
    if event_type == "payment_method_added":
        brand = _code(payload, "brand")
        return f"{brand} card added" if brand else "card added"
    if event_type == "payment_method_removed":
        reason = _code(payload, "reason")
        return f"card removed ({reason})" if reason else "card removed"
    if event_type == "invoice_paid":
        return (
            "billing period advanced"
            if _code(payload, "reset") == "period_advanced"
            else "invoice paid"
        )
    if event_type == "payment_failed":
        state = _code(payload, "dunning_state")
        return f"dunning: {state}" if state else "payment failed"
    if event_type == "billing_closure_settled":
        cancelled = payload.get("subscription_cancelled")
        parts = [
            "subscription cancelled"
            if cancelled is True
            else "no subscription to cancel"
            if cancelled is None
            else "subscription cancel failed"
        ]
        if payload.get("usage_owed") is True:
            parts.append("usage owed")
        error = _code(payload, "error")
        if error:
            parts.append(f"error {error}")
        return ", ".join(parts)
    if event_type == "account_reopened":
        state = _code(payload, "luciel_state")
        if payload.get("luciel_restored") is True:
            return f"Luciel restored ({state})" if state else "Luciel restored"
        return f"Luciel left as it was ({state})" if state else "no Luciel to restore"
    if event_type == "billing_period_anchored":
        frm, to = _code(payload, "from_period_start"), _code(payload, "to_period_start")
        return f"period anchor {frm} → {to}" if frm and to else None
    if event_type == "knowledge_source_edited":
        return _knowledge_edit(payload)
    if event_type == "connection_status_changed":
        action = _code(payload, "action")
        if action:
            what = _code(payload, "action_type") or "item"
            where = _code(payload, "connection_type")
            return f"{action}: {what}" + (f" on {where}" if where else "")
        return _transition(
            {
                "from": payload.get("old_status"),
                "to": payload.get("new_status"),
                "reason": payload.get("reason"),
            },
            note_key="reason",
        )
    return None


@router.get("/audit-log", response_model=list[AuditEventOut])
async def usage_audit_log(
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    subject: Subject = Depends(current_admin),
) -> list[AuditEventOut]:
    """Read-only view of the tenant's hash-chained audit log (§5.2), newest first,
    paged (2026-09-05 audit F170). Non-secret: only event metadata is surfaced; no
    secret material is ever in event_payload (the writers enforce that), and ``detail``
    is rendered by :func:`audit_detail` from code-shaped values only. The chain
    integrity is verifiable via verify_chain."""
    async with tenant_session(subject.admin_id) as s:
        rows = await _audit_rows(s, subject.admin_id, limit=limit, offset=offset)
        return [
            AuditEventOut.model_validate(
                {
                    "eventId": str(r.event_id),
                    "eventType": r.event_type,
                    "at": r.created_at,
                    "detail": audit_detail(r.event_type, r.event_payload or {}),
                    "actorUserId": r.actor_user_id,
                }
            )
            for r in rows
        ]


async def _audit_rows(
    s: AsyncSession, admin_id: uuid.UUID, *, limit: int, offset: int = 0
) -> list[AdminAuditLog]:
    return list(
        (
            await s.execute(
                select(AdminAuditLog)
                .where(AdminAuditLog.admin_id == admin_id)
                .order_by(AdminAuditLog.seq.desc())
                .offset(max(0, offset))
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


@router.get("/export.csv")
async def usage_export_csv(
    view: str = Query(
        ...,
        description=(
            "Which analytics view to export as CSV (Vision §7). One of: "
            + ", ".join(sorted(analytics_service.CSV_VIEWS))
        ),
    ),
    subject: Subject = Depends(current_admin),
) -> Response:
    """CSV download of any analytics view for the selected period (Vision §7:
    "exportable reports (CSV download of any analytics view for the selected
    period)" — available to ALL accounts, no plan gate, consistent with §7's
    "All accounts have access to the full metrics listed above ... and
    exportable reports"). Read-only; issues no new write, so no audit event is
    emitted here — the §5.2 enumeration's `data_export_*` events specifically
    describe the full account export BUNDLE (app/lifecycle/export_bundle.py,
    a different, billing/lifecycle-owned feature), not a single analytics-view
    CSV row-dump; there is no matching event type for this narrower action, and
    BUILD_BRIEF.md forbids inventing one outside the §5.2 enumeration.
    """
    if view not in analytics_service.CSV_VIEWS:
        raise HTTPException(status_code=404, detail=f"Unknown analytics view: {view!r}")

    async with tenant_session(subject.admin_id) as s:
        _out, agg = await _build_overview_out(s, subject)
        audit_rows = (
            await _audit_rows(s, subject.admin_id, limit=200) if view == "audit_log" else None
        )
        csv_text = analytics_service.render_csv(view, agg, audit_rows)
        period = (await resolve_billing_period_start(s, admin_id=subject.admin_id)).isoformat()

    filename = f"analytics_{view}_{period}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
