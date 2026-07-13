"""Wire models for the usage dashboard API (frozen contract: schemas/analytics.ts).

usage/overview returns ``analyticsOverview`` and usage/audit-log returns
``auditEvent[]`` (there is no separate usage schema — reuse the analytics shapes).
camelCase via WireModel. Aggregates only, tenant-scoped, no new PII (Arch §3.9).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.schemas_api.common import WireModel


class EscalationBySignal(WireModel):
    signal: str
    count: int


class ChannelMix(WireModel):
    channel: str
    fraction: float


class AnalyticsOverviewOut(WireModel):
    """Frozen analytics.ts ``analyticsOverview`` — exact field set + camelCase."""

    conversations_this_period: int
    conversations_total: int
    leads_this_period: int
    appointments_booked: int
    response_time_p50_seconds: float
    response_time_p95_seconds: float
    escalations_by_signal: list[EscalationBySignal]
    channel_mix: list[ChannelMix]
    budget_utilization: float
    busiest_times: list[list[int]]


class AuditEventOut(WireModel):
    """Frozen analytics.ts ``auditEvent`` — read-only audit row view (Arch §5.2)."""

    event_id: str
    event_type: str
    at: datetime
    detail: str | None = None
    actor_user_id: uuid.UUID | None = None
