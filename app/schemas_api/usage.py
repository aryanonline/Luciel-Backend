"""Wire models for the usage dashboard API (frozen contract: schemas/analytics.ts).

usage/overview returns ``analyticsOverview`` and usage/audit-log returns
``auditEvent[]`` (there is no separate usage schema — reuse the analytics shapes).
camelCase via WireModel. Aggregates only, tenant-scoped, no new PII (Arch §3.9).

Honest-gap fields (appointmentsBooked, responseTimeP50/P95Seconds,
topKnowledgeSources): these are nullable/empty-with-a-note rather than
hardcoded to 0/[] when the underlying store doesn't exist yet — see
app/analytics/service.py module docstring for exactly which stores are
missing and why. This is a deliberate widening of the wire shape (nullable
where the original schema had non-nullable defaults of 0/[]); the frozen
schemas/analytics.ts Zod schema needs the matching widening (frontend
workstream note — this file only owns the backend Pydantic side).
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


class TopKnowledgeSource(WireModel):
    source_id: str
    name: str
    retrieval_count: int


class ChannelConversionOut(WireModel):
    channel: str
    conversations: int
    leads: int
    conversion_rate: float


class SourceConversionOut(WireModel):
    """Outcome-based conversion by lead source (Vision §7; C14): source = the
    lead's first-contact channel, conversion = admin-marked leads.outcome."""

    source: str
    leads: int
    converted: int
    conversion_rate: float


class AnalyticsOverviewOut(WireModel):
    """Frozen analytics.ts ``analyticsOverview`` — exact field set + camelCase,
    widened with §3.9 fields that were previously hardcoded/missing (see
    app/analytics/service.py for the honest-gap policy)."""

    conversations_this_period: int
    conversations_total: int
    leads_this_period: int
    # Nullable: null + a note when no tool-call/trace store exists yet (never a
    # fabricated 0 — see app/analytics/service.py TOOL_CALL_GAP).
    appointments_booked: int | None
    appointments_booked_note: str | None
    # Nullable: null + a note when no ack-timestamp store exists yet (never a
    # fabricated 0.0 — see app/analytics/service.py RESPONSE_TIME_GAP).
    response_time_p50_seconds: float | None
    response_time_p95_seconds: float | None
    response_time_note: str | None
    escalations_by_signal: list[EscalationBySignal]
    channel_mix: list[ChannelMix]
    budget_utilization: float
    busiest_times: list[list[int]]
    # New in this build (Arch §3.9 "Top knowledge sources used"). Empty + a note
    # when no retrieval-frequency log exists yet (see RETRIEVAL_GAP).
    top_knowledge_sources: list[TopKnowledgeSource]
    top_knowledge_sources_note: str | None
    # New in this build (Vision §7 "conversion by source / channel / service").
    # Channel is real (lead-capture-rate proxy); source/service are honest gaps —
    # no such dimension is persisted anywhere (see CONVERSION_SOURCE_GAP/SERVICE_GAP).
    conversion_by_channel: list[ChannelConversionOut]
    # Real since C14 (leads.outcome, migration 0050 + first-contact channel):
    # outcome-based conversion per lead source. The note is served only if a
    # genuine gap returns; service-type remains the one honest gap (no service
    # taxonomy is modeled anywhere).
    conversion_by_source: list[SourceConversionOut] = []
    conversion_by_source_note: str | None
    conversion_by_service_note: str | None
    # New in this build (harmony fix 2026-08-04 item 4). False means escalations are
    # only reaching the owner via the last-resort account-email fallback (§3.5.4),
    # never a configured primary/secondary contact (§3.5.3) — the FE nudge banner
    # ("configure who gets notified") reads this rather than inferring it from
    # escalation history, which would be empty for a brand-new account with zero fired
    # escalations yet and wrongly imply nothing needs configuring.
    escalation_contact_configured: bool
    # Round 5B item 13: True when a CONFIGURED escalation email contact is
    # currently suppressed (hard bounce / complaint, unresolved by a
    # re-confirmation) — the FE banner "your escalation contact is bouncing"
    # reads this. Escalations meanwhile fall back to the account owner's email.
    escalation_contact_bouncing: bool = False


class AuditEventOut(WireModel):
    """Frozen analytics.ts ``auditEvent`` — read-only audit row view (Arch §5.2)."""

    event_id: str
    event_type: str
    at: datetime
    # Owner-readable one-liner rendered by ``admin/usage.audit_detail`` from an
    # allow-list of code-shaped payload values (round 7 WP-5, item 6): a state
    # transition, a data class + count + policy, a billing move, a connection status
    # change. Never a secret, never PII; null for event types with no renderer.
    detail: str | None = None
    actor_user_id: uuid.UUID | None = None


class AnalyticsCsvViewOut(WireModel):
    """Not itself a wire response body (the endpoint streams text/csv) — kept here
    only to document the valid ``view`` query-param values for §7 CSV export."""

    views: list[str]
