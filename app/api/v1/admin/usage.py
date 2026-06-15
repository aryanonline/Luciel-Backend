"""Budget monitoring + usage dashboard API (DOCTRINE ANCHOR — Architecture §8).

Serves the frozen client (http-admin.ts analytics.*):
  * GET /api/v1/admin/usage/overview   → analyticsOverview (aggregates, §3.9)
  * GET /api/v1/admin/usage/audit-log  → auditEvent[] (read-only, hash-chained §5.2)

Both are admin-authed (verified, §3.7.2), run inside ``tenant_session`` so RLS
scopes every aggregate to the caller's tenant (no new PII), and read existing
stores only — no new write path. The audit-log view reuses the Arc 9/10
hash-chained log and its ``verify_chain`` (read-only, non-secret).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Subject, current_admin
from app.billing import metering
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.conversation import EscalationEvent, Session
from app.models.instance import Instance
from app.models.lead import Lead
from app.runtime.budget import current_billing_period_start
from app.schemas_api.usage import (
    AnalyticsOverviewOut,
    AuditEventOut,
    ChannelMix,
    EscalationBySignal,
)

router = APIRouter(prefix="/api/v1/admin/usage", tags=["usage"])


async def _instance_id(s: AsyncSession, admin_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
    ).scalar_one_or_none()


@router.get("/overview", response_model=AnalyticsOverviewOut)
async def usage_overview(subject: Subject = Depends(current_admin)) -> AnalyticsOverviewOut:
    async with tenant_session(subject.admin_id) as s:
        instance_id = await _instance_id(s, subject.admin_id)

        # conversationsThisPeriod comes from the budget counter (the single source
        # of the count, §3.4.1b) — NOT recomputed from sessions.
        period = current_billing_period_start()
        conversations_this_period = 0
        budget_util = 0.0
        if instance_id is not None:
            budget = await metering.build_budget(
                s, admin_id=subject.admin_id, instance_id=instance_id
            )
            conversations_this_period = budget.conversations_this_period
            # Utilization against the free allowance (≥0; can exceed 1 on PAYG).
            budget_util = (
                budget.conversations_this_period / budget.free_allowance
                if budget.free_allowance
                else 0.0
            )

        conversations_total = (
            await s.execute(select(func.count()).select_from(Session))
        ).scalar_one()
        leads_this_period = (
            await s.execute(select(func.count()).select_from(Lead).where(Lead.created_at >= period))
        ).scalar_one()

        # Escalations grouped by signal this period (aggregate only, §3.9).
        esc_rows = (
            await s.execute(
                select(EscalationEvent.signal, func.count())
                .where(EscalationEvent.fired_at >= period)
                .group_by(EscalationEvent.signal)
            )
        ).all()
        escalations_by_signal = [
            EscalationBySignal.model_validate({"signal": sig, "count": cnt})
            for sig, cnt in esc_rows
        ]

        # Channel mix over all sessions (fraction per channel).
        chan_rows = (
            await s.execute(select(Session.channel, func.count()).group_by(Session.channel))
        ).all()
        total_sessions = sum(cnt for _, cnt in chan_rows) or 0
        channel_mix = [
            ChannelMix.model_validate(
                {"channel": ch, "fraction": (cnt / total_sessions) if total_sessions else 0.0}
            )
            for ch, cnt in chan_rows
        ]

        # FLAGGED: appointment booking, escalation→admin response-time percentiles,
        # and the busiest-times heatmap have no dedicated store yet (the trace store
        # lands later). Surface schema-valid empties/zeros so the wire shape is exact
        # without fabricating numbers (Arch §3.9 — aggregates only, honest gaps).
        return AnalyticsOverviewOut.model_validate(
            {
                "conversationsThisPeriod": conversations_this_period,
                "conversationsTotal": conversations_total,
                "leadsThisPeriod": leads_this_period,
                "appointmentsBooked": 0,
                "responseTimeP50Seconds": 0.0,
                "responseTimeP95Seconds": 0.0,
                "escalationsBySignal": escalations_by_signal,
                "channelMix": channel_mix,
                "budgetUtilization": budget_util,
                "busiestTimes": [],
            }
        )


@router.get("/audit-log", response_model=list[AuditEventOut])
async def usage_audit_log(subject: Subject = Depends(current_admin)) -> list[AuditEventOut]:
    """Read-only view of the tenant's hash-chained audit log (§5.2). Non-secret:
    only event metadata is surfaced; no secret material is ever in event_payload
    (the writers enforce that). The chain integrity is verifiable via verify_chain."""
    async with tenant_session(subject.admin_id) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog)
                    .where(AdminAuditLog.admin_id == subject.admin_id)
                    .order_by(AdminAuditLog.seq.desc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        return [
            AuditEventOut.model_validate(
                {
                    "eventId": str(r.event_id),
                    "eventType": r.event_type,
                    "at": r.created_at,
                    "actorUserId": r.actor_user_id,
                }
            )
            for r in rows
        ]
